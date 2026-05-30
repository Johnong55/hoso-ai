"""
Reset các DocumentSource bị kẹt ở crawl_status='crawling' về 'pending'.

Dùng khi dừng crawl giữa chừng (Ctrl+C worker) → source kẹt trạng thái "Đang crawl".
KHÔNG xóa chunks đã embed — chỉ đổi trạng thái để có thể trigger crawl lại.

Usage:
    python -m scripts.reset_crawl_status
"""
import asyncio
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from sqlalchemy import select, update

from app.db.base import AsyncSessionLocal
from app.models.document import CrawlStatus, DocumentSource


async def main() -> None:
    async with AsyncSessionLocal() as db:
        stuck = (await db.execute(
            select(DocumentSource.id, DocumentSource.title)
            .where(DocumentSource.crawl_status == CrawlStatus.CRAWLING)
        )).all()

        if not stuck:
            logger.info("Không có source nào đang kẹt 'crawling'.")
            return

        for sid, title in stuck:
            logger.info(f"  reset: {title} ({sid})")

        await db.execute(
            update(DocumentSource)
            .where(DocumentSource.crawl_status == CrawlStatus.CRAWLING)
            .values(crawl_status=CrawlStatus.PENDING)
        )
        await db.commit()
        logger.info(f"Đã reset {len(stuck)} source về 'pending'.")


if __name__ == "__main__":
    asyncio.run(main())
