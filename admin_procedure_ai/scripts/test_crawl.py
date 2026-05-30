# scripts/test_crawl.py
"""
Chạy thử crawler xlsx-based để kiểm tra trước khi deploy.

Usage:
    python -m scripts.test_crawl                       # test 1 mã cụ thể (1.015028)
    python -m scripts.test_crawl --code 1.000123       # test mã khác
    python -m scripts.test_crawl --list-codes          # liệt kê mã từ folder xlsx
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from loguru import logger

from app.core.config import settings
from app.crawler.sources.dvcqg_xlsx import (
    collect_all_codes,
    collect_all_codes_online,
    fetch_agency_list,
    fetch_and_parse_procedure,
)


async def test_single_code(code: str) -> None:
    print(f"\n{'='*60}\nTest fetch: code={code}\n{'='*60}")

    async with httpx.AsyncClient() as client:
        parsed = await fetch_and_parse_procedure(client, code)

    if not parsed:
        print("❌ Không lấy được dữ liệu")
        return

    print(f"✅ idTTHC:              {parsed.get('id_tthc')}")
    print(f"   Tên:                 {(parsed.get('name') or '')[:120]}")
    print(f"   Mã:                  {parsed.get('code')}")
    print(f"   Lĩnh vực:            {parsed.get('domain')}")
    print(f"   Cấp:                 {parsed.get('authority_level_text')}")
    print(f"   Cơ quan thực hiện:   {parsed.get('implementing_agency')}")
    print(f"   Kết quả:             {(parsed.get('result') or '')[:100]}")
    print(f"   Phí summary:         {parsed.get('fee_summary')}")
    print(f"   Thời hạn:            {parsed.get('processing_time')}")
    print(f"   Fees:                {len(parsed.get('fees', []))} mức")
    print(f"   Requirements:        {len(parsed.get('requirements', []))} giấy tờ")
    print(f"   Legal basis:         {len(parsed.get('legal_basis_items', []))} văn bản")
    print(f"   Steps_text:          {len(parsed.get('steps_text') or '')} chars")

    print(f"\n   --- 3 fees đầu ---")
    for f in parsed.get("fees", [])[:3]:
        print(f"     [{f['submission_method']}] {f.get('amount_text')} — {(f.get('description') or '')[:80]}")

    print(f"\n   --- 3 requirements đầu ---")
    for r in parsed.get("requirements", [])[:3]:
        print(f"     • {r.get('name')[:100]}")
        print(f"       case_group: {(r.get('case_group') or '')[:80]}")
        print(f"       form: {r.get('form_name')} | qty: {r.get('quantity')}")


def list_codes_local() -> None:
    metas = collect_all_codes()
    print(f"[LOCAL] Total: {len(metas)} codes from {settings.XLSX_DATA_DIR}\n")
    for m in metas[:30]:
        print(f"  {m['code']:<14} {(m.get('name_xlsx') or '')[:90]:<90} [{m.get('source_xlsx')}]")
    if len(metas) > 30:
        print(f"  ... và {len(metas)-30} mã khác")


async def list_agencies() -> None:
    async with httpx.AsyncClient() as client:
        agencies = await fetch_agency_list(client)
    print(f"Agencies ({len(agencies)}):\n")
    for a in agencies:
        print(f"  id={a['id']:<12} code={a['code']:<18} {a['name']}")


async def list_codes_online(agency_id: str | None) -> None:
    async with httpx.AsyncClient() as client:
        metas = await collect_all_codes_online(client, agency_id=agency_id)
    print(f"\n[ONLINE] Total: {len(metas)} codes\n")
    for m in metas[:30]:
        print(f"  {m['code']:<14} {(m.get('name_xlsx') or '')[:80]:<80} [{m.get('source_xlsx')}]")
    if len(metas) > 30:
        print(f"  ... và {len(metas)-30} mã khác")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--code", default="1.015028", help="Mã TTHC cần test parse")
    p.add_argument("--list-codes", action="store_true", help="[LOCAL] Liệt kê mã từ xlsx folder")
    p.add_argument("--list-agencies", action="store_true", help="[ONLINE] Liệt kê cơ quan qua API")
    p.add_argument("--list-online", nargs="?", const="", default=None,
                   metavar="AGENCY_ID",
                   help="[ONLINE] Liệt kê mã: bỏ trống=tất cả, hoặc truyền agency_id")
    args = p.parse_args()

    if args.list_codes:
        list_codes_local()
    elif args.list_agencies:
        asyncio.run(list_agencies())
    elif args.list_online is not None:
        asyncio.run(list_codes_online(args.list_online or None))
    else:
        asyncio.run(test_single_code(args.code))


if __name__ == "__main__":
    main()
