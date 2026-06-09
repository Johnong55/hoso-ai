"""
Phase 11 — Backfill: tải + parse file biểu mẫu cho các ProcedureRequirement
đã crawl trước Phase 11, lưu form_content_text + form_fields_json vào DB.

Pipeline:
  1. Query distinct procedure_id có ProcedureRequirement.form_url
     AND form_parse_status IS NULL (chưa parse).
  2. Cho mỗi procedure, gọi Celery task parse_procedure_forms.delay(id)
     → worker tải + parse + lưu DB (concurrency=3 trong task).

Usage:
    python -m scripts.parse_existing_forms                  # dry-run
    python -m scripts.parse_existing_forms --confirm        # enqueue hết
    python -m scripts.parse_existing_forms --confirm --limit 50  # test 50 proc
    python -m scripts.parse_existing_forms --sync --confirm --limit 5
        # chạy đồng bộ (không qua Celery) để debug
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from loguru import logger
from sqlalchemy import distinct, select

from app.db.base import AsyncSessionLocal, engine
from app.models.procedure import ProcedureRequirement


async def main(
    confirm: bool,
    limit: int | None,
    sync: bool,
    re_parse: bool,
) -> None:
    try:
        async with AsyncSessionLocal() as db:
            # Procedures chưa parse forms (hoặc tất cả nếu --re-parse)
            stmt = (
                select(distinct(ProcedureRequirement.procedure_id))
                .where(ProcedureRequirement.form_url.is_not(None))
            )
            if not re_parse:
                stmt = stmt.where(ProcedureRequirement.form_parse_status.is_(None))
            if limit:
                stmt = stmt.limit(limit)
            rows = (await db.execute(stmt)).all()
            procedure_ids = [r[0] for r in rows]

        logger.info(
            f"Procedures cần parse forms: {len(procedure_ids)} "
            f"| re_parse={re_parse} | limit={limit}"
        )
        if not procedure_ids:
            logger.info("Không có procedure nào cần parse. Bỏ qua.")
            return

        if not confirm:
            logger.warning(
                "Dry-run. Chạy lại với --confirm để enqueue Celery tasks."
            )
            return

        if sync:
            # Đồng bộ — gọi trực tiếp coroutine (debug local)
            from app.worker.tasks import _parse_procedure_forms_async

            start = time.monotonic()
            ok = 0
            failed = 0
            for i, pid in enumerate(procedure_ids, 1):
                try:
                    res = await _parse_procedure_forms_async(pid)
                    if res.get("status") == "done":
                        ok += 1
                    else:
                        failed += 1
                    logger.info(
                        f"  [{i}/{len(procedure_ids)}] proc={pid[:8]} | {res}"
                    )
                except Exception as e:
                    failed += 1
                    logger.warning(f"  [{i}/{len(procedure_ids)}] FAIL | {e}")
            elapsed = time.monotonic() - start
            logger.info(
                f"Sync done | ok={ok} failed={failed} | {elapsed:.1f}s "
                f"| avg={elapsed/max(1,len(procedure_ids)):.2f}s/proc"
            )
        else:
            # Async qua Celery — broker fan-out, worker xử lý concurrent
            from app.worker.tasks import parse_procedure_forms

            for i, pid in enumerate(procedure_ids, 1):
                parse_procedure_forms.delay(pid)
                if i % 100 == 0:
                    logger.info(f"  enqueued {i}/{len(procedure_ids)}")
            logger.info(
                f"Enqueued {len(procedure_ids)} Celery tasks. "
                f"Theo dõi worker log: docker compose -f docker-compose.http.yml "
                f"logs -f worker"
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true",
                        help="Apply (không có flag = dry-run)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Giới hạn số procedure xử lý (test)")
    parser.add_argument("--sync", action="store_true",
                        help="Chạy đồng bộ thay vì enqueue Celery (debug)")
    parser.add_argument("--re-parse", action="store_true",
                        help="Re-parse cả procedure đã parse rồi (default chỉ status=NULL)")
    args = parser.parse_args()
    asyncio.run(main(
        confirm=args.confirm,
        limit=args.limit,
        sync=args.sync,
        re_parse=args.re_parse,
    ))
