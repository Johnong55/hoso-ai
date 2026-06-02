"""
Smoke test: probe end-to-end 1 thủ tục từ JSON API mới.

Chạy:  python -m scripts.probe_dvcqg_json [<code|UUID>]
Mặc định: 1.001020 (đăng ký khai sinh — luôn có data)

Kiểm tra:
  1. Warmup + list-all (status 201)
  2. Detail (status 200/201)
  3. Parser → dict shape khớp ProcedureChunker
  4. Download 1 attachment (nếu có)
  5. Chunker tạo chunks không lỗi
"""
from __future__ import annotations

import asyncio
import sys

# Force UTF-8 stdout trên Windows console (cp1252 mặc định không in được tiếng Việt)
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

import httpx
from loguru import logger

from app.crawler.parsers.dvcqg_json_parser import parse_formality_json
from app.crawler.sources.dvcqg_json import (
    DETAIL_URL,
    LIST_URL,
    HEADERS,
    _warmup,
    download_attachment,
    fetch_and_parse_procedure,
    get_procedure_detail,
    list_procedures_page,
)


async def main(target: str = "1.001020") -> int:
    print(f"\n=== PROBE DVCQG JSON | target={target} ===\n")

    async with httpx.AsyncClient(http2=False, follow_redirects=True, timeout=30) as c:
        # 1. Warmup
        await _warmup(c)
        print("[1] warmup OK")

        # 2. List (search) — verify status 201
        r = await c.post(
            LIST_URL,
            headers=HEADERS,
            json={"limit": 5, "lastId": "", "q": target,
                  "categoryId": "", "departmentCode": ""},
        )
        print(f"[2] list status={r.status_code}")
        assert r.status_code in (200, 201), f"list returned {r.status_code}: {r.text[:200]}"
        body = r.json()
        items = (body.get("data") or {}).get("items") or []
        print(f"    items returned: {len(items)}; total: {(body.get('data') or {}).get('total')}")
        if not items:
            print("    !! no items, abort")
            return 1
        item = items[0]
        print(f"    first item: id={item.get('id')} code={item.get('code')} "
              f"name={(item.get('name') or '')[:60]}...")

        # 3. Detail — verify status 200/201
        r = await c.post(
            DETAIL_URL, headers=HEADERS, json={"id": item["id"]}
        )
        print(f"[3] detail status={r.status_code}")
        assert r.status_code in (200, 201), f"detail returned {r.status_code}: {r.text[:200]}"
        detail = (r.json() or {}).get("data") or {}
        print(f"    keys: {sorted(detail.keys())[:12]}...")
        print(f"    updatedAt: {detail.get('updatedAt')}")

        # 4. Parse
        parsed = parse_formality_json(detail)
        print(f"[4] parsed.code={parsed['code']} name={parsed['name'][:60]}...")
        print(f"    source_updated_at={parsed['source_updated_at']}")
        print(f"    domain={parsed['domain']}")
        print(f"    processing_time={parsed['processing_time']}")
        print(f"    fee_summary={parsed['fee_summary']}")
        print(f"    fees: {len(parsed['fees'])} rows")
        print(f"    requirements: {len(parsed['requirements'])} rows")
        print(f"    steps_text len: {len(parsed['steps_text'] or '')}")
        if parsed['fees']:
            print(f"    sample fee: {parsed['fees'][0]}")
        if parsed['requirements']:
            print(f"    sample req: name={parsed['requirements'][0]['name'][:60]}... "
                  f"form_name={parsed['requirements'][0]['form_name']} "
                  f"form_url={(parsed['requirements'][0].get('form_url') or '')[:80]}...")

        # 5. Attachment download (nếu có form_url)
        first_form_url = None
        for r_ in parsed['requirements']:
            if r_.get('form_url'):
                first_form_url = r_['form_url']
                first_form_name = r_['form_name']
                break

        if first_form_url:
            print(f"[5] download attachment: {first_form_name}")
            # Extract fileId từ URL
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(first_form_url).query)
            file_id = (qs.get("fileId") or [None])[0]
            data = await download_attachment(c, file_id)
            if data:
                magic = data[:4]
                print(f"    got {len(data)} bytes, magic={magic!r}")
                # docx=PK\x03\x04 (ZIP), pdf=%PDF, legacy .doc=\xd0\xcf\x11\xe0 (CFB)
                if magic in (b"PK\x03\x04", b"%PDF") or data[:2] == b"\xd0\xcf":
                    print("    [OK] valid file magic")
                else:
                    print(f"    [WARN] unexpected magic for {first_form_name}")
            else:
                print("    [WARN] download returned no data")
        else:
            print("[5] no form_url → skip attachment download")

        # 6. Chunker compat
        from app.rag.chunking.strategy import ProcedureChunker
        parsed["id"] = "test-id"
        parsed["authority_level"] = "central"
        chunks = ProcedureChunker().chunk_procedure(parsed)
        print(f"[6] chunker produced {len(chunks)} chunks")
        type_counts: dict[str, int] = {}
        for ch in chunks:
            k = str(ch.chunk_type)
            type_counts[k] = type_counts.get(k, 0) + 1
        for k, v in type_counts.items():
            print(f"    - {k}: {v}")
        assert len(chunks) > 0, "no chunks produced"

        print("\n=== PROBE OK ===\n")
        return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "1.001020"
    rc = asyncio.run(main(arg))
    sys.exit(rc)
