"""
Backfill procedures.formality_id cho data đã crawl trước Phase 7.

Với mỗi procedure trong DB chưa có formality_id, gọi API DVCQG
list-all-formality với q=<code> để lấy UUID, rồi UPDATE.

Usage:
    python -m scripts.backfill_formality_id           # dry-run, in số rows
    python -m scripts.backfill_formality_id --confirm # apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

import httpx
from loguru import logger
from sqlalchemy import select

from app.crawler.sources.dvcqg_json import _warmup, list_procedures_page
from app.db.base import AsyncSessionLocal, engine
from app.models.procedure import Procedure


async def main(confirm: bool, batch_size: int = 50) -> None:
    try:
        # Procedures cần backfill
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Procedure).where(Procedure.formality_id.is_(None))
            )).scalars().all()
        logger.info(f"Procedures cần backfill formality_id: {len(rows)}")
        if not rows:
            return

        if not confirm:
            for p in rows[:5]:
                logger.info(f"  sample: code={p.code} name={p.name[:60]}")
            logger.warning("Dry-run. Chạy lại với --confirm để gọi API + apply.")
            return

        async with httpx.AsyncClient(
            http2=False, follow_redirects=True, timeout=30
        ) as client:
            await _warmup(client)

            # Map code → formality_id qua API search
            ok = 0
            failed = 0
            async with AsyncSessionLocal() as db:
                for i, p in enumerate(rows, 1):
                    data = await list_procedures_page(client, "", limit=5, q=p.code)
                    if not data or not data.get("items"):
                        failed += 1
                        logger.warning(f"  [{i}/{len(rows)}] code={p.code} NOT FOUND")
                        continue
                    # Find exact code match
                    fid = None
                    for it in data["items"]:
                        if (it.get("code") or "").strip() == p.code:
                            fid = (it.get("id") or "").strip()
                            break
                    if not fid:
                        fid = (data["items"][0].get("id") or "").strip()
                    if not fid:
                        failed += 1
                        continue

                    # Update in this session
                    p_db = (await db.execute(
                        select(Procedure).where(Procedure.id == p.id)
                    )).scalar_one()
                    p_db.formality_id = fid
                    ok += 1
                    if i % 10 == 0:
                        await db.commit()
                        logger.info(f"  progress {i}/{len(rows)} | ok={ok} fail={failed}")

                await db.commit()
            logger.info(f"Done. ok={ok} failed={failed}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.confirm))
