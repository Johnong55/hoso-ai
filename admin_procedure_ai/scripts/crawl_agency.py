"""
Crawl + embed THỦ CÔNG từng bộ/ngành (chạy đồng bộ trong process này).

Không cần celery worker — task chạy eager (.apply()) ngay trong tiến trình.
Dùng để crawl & chunking từng bộ một, dễ theo dõi tiến độ và kiểm soát quota.

Yêu cầu: đã chạy `python -m scripts.seed_agency_sources --confirm` để có sources.

Usage:
    python -m scripts.crawl_agency --list                       # liệt kê sources
    python -m scripts.crawl_agency --source-id <uuid>           # crawl 1 source
    python -m scripts.crawl_agency --agency "Tập đoàn Điện lực"  # crawl theo tên
    python -m scripts.crawl_agency --all-sequential             # crawl lần lượt tất cả
    python -m scripts.crawl_agency --all-sequential --pause 30  # nghỉ 30s giữa các bộ
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

# Windows console mặc định cp1252 → in tiếng Việt lỗi. Ép UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from sqlalchemy import select

from app.db.base import AsyncSessionLocal
from app.models.document import DocumentSource


async def _list_sources() -> list[tuple]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(
                DocumentSource.id,
                DocumentSource.title,
                DocumentSource.source_url,
                DocumentSource.crawl_status,
            ).where(DocumentSource.source_type == "dvcqg_xlsx")
        )).all()
    return rows


def _run_crawl(source_id: str) -> dict:
    """Chạy task crawl đồng bộ (eager) trong process hiện tại."""
    from app.worker.tasks import crawl_and_embed_procedure
    result = crawl_and_embed_procedure.apply(args=[source_id])
    return result.get() if result.successful() else {"status": "failed", "error": str(result.result)}


async def list_cmd() -> None:
    rows = await _list_sources()
    if not rows:
        print("Chưa có source dvcqg_xlsx. Chạy: python -m scripts.seed_agency_sources --confirm")
        return
    print(f"{len(rows)} sources:\n")
    for sid, title, surl, status in rows:
        print(f"  {sid}  [{str(surl):<10}]  {str(status):<10}  {title}")


async def find_source_by_agency(name: str) -> str | None:
    rows = await _list_sources()
    nl = name.lower()
    for sid, title, surl, _ in rows:
        if title and nl in title.lower():
            return sid
    return None


def crawl_one(source_id: str, title: str = "") -> None:
    print(f"\n{'='*70}\nCrawling: {title or source_id}\n{'='*70}")
    t0 = time.time()
    res = _run_crawl(source_id)
    dt = time.time() - t0
    print(f"Result: {res}  ({dt:.1f}s)")


def all_sequential(pause: int) -> None:
    # Lấy danh sách source TRƯỚC (đóng event loop) rồi mới crawl đồng bộ —
    # tránh tạo event loop lồng nhau (crawl_one → .apply() → _run_async tạo loop mới).
    rows = asyncio.run(_list_sources())
    print(f"Sẽ crawl lần lượt {len(rows)} bộ (pause={pause}s giữa các bộ)\n")
    for i, (sid, title, surl, _) in enumerate(rows, 1):
        print(f"\n[{i}/{len(rows)}] {title}")
        crawl_one(sid, title)
        if i < len(rows) and pause > 0:
            print(f"  ...nghỉ {pause}s (tránh rate limit)...")
            time.sleep(pause)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--list", action="store_true", help="Liệt kê sources")
    p.add_argument("--source-id", help="Crawl 1 source theo uuid")
    p.add_argument("--agency", help="Crawl theo tên cơ quan (substring)")
    p.add_argument("--all-sequential", action="store_true", help="Crawl lần lượt tất cả sources")
    p.add_argument("--pause", type=int, default=10, help="Số giây nghỉ giữa các bộ (mặc định 10)")
    args = p.parse_args()

    if args.list:
        asyncio.run(list_cmd())
    elif args.source_id:
        crawl_one(args.source_id)
    elif args.agency:
        sid = asyncio.run(find_source_by_agency(args.agency))
        if not sid:
            print(f"Không tìm thấy source khớp '{args.agency}'. Chạy --list để xem.")
            return
        crawl_one(sid, args.agency)
    elif args.all_sequential:
        all_sequential(args.pause)
    else:
        print("Chọn 1 trong: --list | --source-id <id> | --agency <tên> | --all-sequential")


if __name__ == "__main__":
    main()
