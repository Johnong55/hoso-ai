"""
Rewrite ProcedureRequirement.form_url cũ (DVCQG direct GET, không click được)
sang URL proxy mới /api/v1/forms/<file_id>?name=<filename>.

Trước:  https://dichvucong.gov.vn/api/v1/submitting/preview-attachment?fileId=ABC
Sau:    /api/v1/forms/ABC?name=<form_name>

Dùng cho data đã crawl bằng phiên bản parser cũ. Idempotent — rows đã đúng format
sẽ skip.

Usage:
    python -m scripts.rewrite_form_urls               # dry-run
    python -m scripts.rewrite_form_urls --confirm     # apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from urllib.parse import parse_qs, quote, urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from loguru import logger
from sqlalchemy import select

from app.db.base import AsyncSessionLocal, engine
from app.models.procedure import ProcedureRequirement


DVCQG_HOST = "dichvucong.gov.vn"
NEW_PREFIX = "/api/v1/forms/"


def _rewrite(old_url: str, form_name: str | None) -> str | None:
    """
    Return new URL nếu cần rewrite, else None (đã đúng format hoặc không hợp lệ).
    """
    if not old_url:
        return None
    if old_url.startswith(NEW_PREFIX):
        return None  # already migrated
    if DVCQG_HOST not in old_url:
        return None  # unknown format, leave alone

    parsed = urlparse(old_url)
    qs = parse_qs(parsed.query)
    file_id = (qs.get("fileId") or [None])[0]
    if not file_id:
        return None

    new_url = f"{NEW_PREFIX}{file_id}"
    if form_name:
        new_url += f"?name={quote(form_name)}"
    return new_url


async def main(confirm: bool) -> None:
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(ProcedureRequirement).where(
                    ProcedureRequirement.form_url.is_not(None)
                )
            )).scalars().all()

            to_update: list[tuple[ProcedureRequirement, str]] = []
            for r in rows:
                new_url = _rewrite(r.form_url or "", r.form_name)
                if new_url and new_url != r.form_url:
                    to_update.append((r, new_url))

            logger.info(
                f"Scanned {len(rows)} requirements with form_url, "
                f"{len(to_update)} need rewriting"
            )

            if to_update:
                # Sample 3 dòng cho dễ debug
                for r, new in to_update[:3]:
                    logger.info(f"  {r.id} | name={r.form_name!r}")
                    logger.info(f"    old: {r.form_url}")
                    logger.info(f"    new: {new}")

            if not confirm:
                logger.warning("Dry-run. Chạy lại với --confirm để apply.")
                return

            for r, new in to_update:
                r.form_url = new
            await db.commit()
            logger.info(f"Done. Rewrote {len(to_update)} rows.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.confirm))
