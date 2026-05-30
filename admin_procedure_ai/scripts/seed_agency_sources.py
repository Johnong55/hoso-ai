"""
Tạo sẵn 1 DocumentSource cho mỗi cơ quan (bộ/ngành) từ API DVCQG.

Mục đích: thay vì crawl tất cả trong 1 source ("all"), ta tách thành 25 sources
— mỗi bộ/ngành 1 source — để crawl & chunking ĐỘC LẬP từng bộ, dễ kiểm soát
quota Gemini và dễ retry nếu 1 bộ lỗi.

Sau khi seed:
  - Mỗi source có source_url = agency_id, title = tên cơ quan
  - Trigger crawl từng source qua UI (nút Crawl) hoặc:
      python -m scripts.crawl_agency --source-id <id>
      python -m scripts.crawl_agency --all-sequential

Usage:
    python -m scripts.seed_agency_sources              # dry-run, chỉ in danh sách
    python -m scripts.seed_agency_sources --confirm    # thực sự tạo/cập nhật sources
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Windows console mặc định cp1252 → in tiếng Việt lỗi. Ép UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from loguru import logger
from sqlalchemy import select

from app.db.base import AsyncSessionLocal
from app.models.document import CrawlFrequency, DocumentSource
from app.crawler.sources.dvcqg_xlsx import fetch_agency_list


async def seed(confirm: bool) -> None:
    async with httpx.AsyncClient() as client:
        agencies = await fetch_agency_list(client)

    if not agencies:
        logger.error("Không lấy được danh sách cơ quan từ API")
        return

    logger.info(f"Fetched {len(agencies)} agencies")

    if not confirm:
        print("\n[DRY-RUN] Sẽ tạo/cập nhật các source sau (chạy --confirm để thực thi):\n")
        for a in agencies:
            print(f"  source_url={a['id']:<12} title={a['name']}")
        print(f"\nTổng: {len(agencies)} sources. Chạy lại với --confirm để tạo.")
        return

    created, updated = 0, 0
    async with AsyncSessionLocal() as db:
        for a in agencies:
            # Idempotent: dedupe theo source_url = agency_id
            existing = (await db.execute(
                select(DocumentSource).where(DocumentSource.source_url == a["id"])
            )).scalar_one_or_none()

            if existing:
                existing.title = a["name"][:300]
                existing.is_active = True
                updated += 1
            else:
                db.add(DocumentSource(
                    title=a["name"][:300],
                    source_url=a["id"],          # agency_id → task tự resolve sang online mode
                    source_type="dvcqg_xlsx",
                    is_active=True,
                    crawl_frequency=CrawlFrequency.MANUAL,
                ))
                created += 1
        await db.commit()

    logger.info(f"Seed done | created={created} | updated={updated}")
    print("\nXem source_id để trigger crawl:")
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(DocumentSource.id, DocumentSource.title, DocumentSource.source_url)
            .where(DocumentSource.source_type == "dvcqg_xlsx")
        )).all()
        for sid, title, surl in rows:
            print(f"  {sid}  [{surl:<10}]  {title}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--confirm", action="store_true", help="Thực sự tạo/cập nhật sources")
    args = p.parse_args()
    asyncio.run(seed(args.confirm))


if __name__ == "__main__":
    main()
