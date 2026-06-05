"""
Crawler dùng JSON API mới của Cổng Dịch vụ công Quốc gia.

Endpoints (tất cả POST, server trả status 201 cho list/detail):
  - POST /api/v1/submitting/formality/list-all-formality-by-citizen
      Body: {limit, lastId, q, categoryId, departmentCode}
      Response.data.items[], lastId (cursor), total
  - POST /api/v1/configuring/formality/get-formality-by-citizen
      Body: {id: <UUID>}
      Response.data: full detail (xem dvcqg_json_parser)
  - GET  /api/v1/submitting/preview-attachment?fileId=<UUID>
      Response: bytes file biểu mẫu

Anti-bot: server đôi khi 403 / drop connection nếu thiếu header Sec-* hoặc cookie.
Warmup bằng GET /thu-tuc-hanh-chinh trước, dùng cùng 1 client cho cả session.

Retry: httpx.RemoteProtocolError xảy ra ngẫu nhiên → retry 2-3 lần với backoff.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Iterable

import httpx
from loguru import logger

from app.core.config import settings
from app.crawler.parsers.dvcqg_json_parser import parse_formality_json


BASE = "https://dichvucong.gov.vn"
LIST_URL = f"{BASE}/api/v1/submitting/formality/list-all-formality-by-citizen"
DETAIL_URL = f"{BASE}/api/v1/configuring/formality/get-formality-by-citizen"
DEPARTMENTS_URL = f"{BASE}/api/v1/configuring/citizen/department/list-with-location"
ATTACHMENT_URL = f"{BASE}/api/v1/submitting/preview-attachment"
WARMUP_URL = f"{BASE}/thu-tuc-hanh-chinh"


HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9",
    "Content-Type": "application/json",
    "Origin": BASE,
    "Referer": f"{BASE}/thu-tuc-hanh-chinh",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def _warmup(client: httpx.AsyncClient) -> None:
    """GET trang HTML chính để set cookie trước khi gọi API JSON."""
    try:
        r = await client.get(
            WARMUP_URL,
            headers={
                **HEADERS,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
            },
            timeout=settings.CRAWLER_TIMEOUT,
        )
        if r.status_code >= 400:
            logger.warning(f"DVCQG | warmup non-200 | status={r.status_code}")
    except Exception as e:
        # Không chặn — đôi khi vẫn gọi API được dù warmup fail
        logger.warning(f"DVCQG | warmup failed | {e}")


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    *,
    retries: int = 3,
    timeout: float | None = None,
) -> httpx.Response | None:
    """
    POST với retry cho RemoteProtocolError + 5xx + 429.
    Trả về response (status 200 hoặc 201), hoặc None nếu retry hết vẫn fail.
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = await client.post(
                url,
                json=payload,
                headers=HEADERS,
                timeout=timeout or settings.CRAWLER_TIMEOUT,
            )
            # API DVCQG trả 201 cho POST thành công
            if r.status_code in (200, 201):
                return r
            if r.status_code in (429, 502, 503, 504):
                # transient → retry
                backoff = (attempt + 1) * 1.5
                logger.warning(
                    f"DVCQG | POST {url} | status={r.status_code} retry in {backoff}s"
                )
                await asyncio.sleep(backoff)
                continue
            # 4xx khác → không retry
            logger.warning(
                f"DVCQG | POST {url} | status={r.status_code} body={r.text[:200]!r}"
            )
            return None
        except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError) as e:
            last_exc = e
            backoff = (attempt + 1) * 1.5
            logger.warning(f"DVCQG | POST {url} | {type(e).__name__}: {e} retry in {backoff}s")
            await asyncio.sleep(backoff)
        except Exception as e:
            logger.warning(f"DVCQG | POST {url} | fatal {type(e).__name__}: {e}")
            return None

    logger.warning(f"DVCQG | POST {url} | giving up after {retries} attempts: {last_exc}")
    return None


# ── List + paginate ───────────────────────────────────────────────────────────

async def list_procedures_page(
    client: httpx.AsyncClient,
    last_id: str = "",
    *,
    limit: int = 50,
    q: str = "",
    category_id: str = "",
    department_code: str = "",
) -> dict[str, Any] | None:
    """
    Một page list-all. Trả về dict {items, lastId, total} hoặc None nếu fail.
    """
    payload = {
        "limit": limit,
        "lastId": last_id,
        "q": q,
        "categoryId": category_id,
        "departmentCode": department_code,
    }
    r = await _post_with_retry(client, LIST_URL, payload)
    if r is None:
        return None
    try:
        body = r.json()
    except Exception as e:
        logger.warning(f"DVCQG | list page json decode failed | {e}")
        return None
    if body.get("code") != "OK":
        logger.warning(f"DVCQG | list page non-OK | code={body.get('code')}")
        return None
    return body.get("data") or {}


async def discover_all_procedures(
    client: httpx.AsyncClient,
    *,
    department_code: str = "",
    q: str = "",
    page_size: int = 50,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    """
    Paginate list-all-formality. Stop khi `items` rỗng hoặc lastId không tiến.

    Filter cơ quan dùng `department_code` (vd "G19", "D01"). Lấy code từ
    `fetch_agency_list` → server lọc thật sự, không phải client-side.
    """
    out: list[dict[str, Any]] = []
    last_id = ""
    page_num = 0
    while True:
        page_num += 1
        if max_pages is not None and page_num > max_pages:
            logger.info(f"DVCQG | discover | hit max_pages={max_pages}, stop")
            break

        data = await list_procedures_page(
            client, last_id, limit=page_size, q=q, department_code=department_code
        )
        if data is None:
            logger.warning(f"DVCQG | discover | page {page_num} failed, stop")
            break

        items = data.get("items") or []
        if not items:
            break

        out.extend(items)

        new_last = data.get("lastId") or ""
        if not new_last or new_last == last_id:
            break
        last_id = new_last
        logger.debug(
            f"DVCQG | discover | page={page_num} got={len(items)} "
            f"accum={len(out)} total={data.get('total')}"
        )

    logger.info(
        f"DVCQG | discover | done | items={len(out)} "
        f"(dept_code={department_code!r} q={q!r})"
    )
    return out


# ── Detail ────────────────────────────────────────────────────────────────────

async def get_procedure_detail(
    client: httpx.AsyncClient,
    procedure_id: str,
) -> dict[str, Any] | None:
    """POST get-formality-by-citizen → response.data (raw JSON)."""
    r = await _post_with_retry(client, DETAIL_URL, {"id": procedure_id})
    if r is None:
        return None
    try:
        body = r.json()
    except Exception as e:
        logger.warning(f"DVCQG | detail json decode failed | id={procedure_id} | {e}")
        return None
    if body.get("code") != "OK":
        logger.warning(
            f"DVCQG | detail non-OK | id={procedure_id} | code={body.get('code')}"
        )
        return None
    return body.get("data")


# ── Agencies ──────────────────────────────────────────────────────────────────

async def fetch_agency_list(
    client: httpx.AsyncClient,
    *,
    levels: list[str] | None = None,
    page_size: int = 50,
) -> list[dict[str, Any]]:
    """
    Lấy danh sách cơ quan từ endpoint chính thức
    `/configuring/citizen/department/list-with-location`.

    `levels`: lọc theo cấp. Mặc định ["MINISTRY"] (chỉ Bộ/cơ quan TW).
    Pass ["PROVINCE"] để lấy UBND tỉnh, hoặc None để không filter.

    Trả về list {id, name, code, level, hasChild} — `code` chính là
    `departmentCode` dùng được trong list-all body để filter server-side.
    """
    out: list[dict[str, Any]] = []
    last_id = ""
    levels_payload = levels if levels is not None else ["MINISTRY"]
    # `type` body field: server đòi 1 string; nếu nhiều levels thì pass cái đầu
    type_field = levels_payload[0] if levels_payload else "MINISTRY"

    while True:
        payload: dict[str, Any] = {
            "direction": "DESC",
            "order": "",
            "type": type_field,
            "agencyLevel": "level_1",
            "lastId": last_id,
            "levels": levels_payload,
            "limit": page_size,
        }
        r = await _post_with_retry(client, DEPARTMENTS_URL, payload)
        if r is None:
            break
        try:
            body = r.json()
        except Exception as e:
            logger.warning(f"DVCQG | agencies json decode failed | {e}")
            break
        if body.get("code") != "OK":
            logger.warning(f"DVCQG | agencies non-OK | code={body.get('code')}")
            break

        data = body.get("data") or {}
        rows = data.get("rows") or []
        if not rows:
            break
        for row in rows:
            out.append({
                "id": str(row.get("id") or "").strip(),
                "name": (row.get("name") or "").strip(),
                "code": (row.get("code") or "").strip(),
                "level": (row.get("level") or "").strip(),
                "has_child": bool(row.get("hasChild")),
            })

        new_last = data.get("lastId") or ""
        if not new_last or new_last == last_id:
            break
        last_id = new_last

    # Dedupe by code (safety; thường server đã unique)
    seen_codes: set[str] = set()
    unique: list[dict[str, Any]] = []
    for a in out:
        if a["code"] and a["code"] in seen_codes:
            continue
        seen_codes.add(a["code"])
        unique.append(a)
    unique.sort(key=lambda a: a["name"].lower())
    logger.info(
        f"DVCQG | fetch_agency_list | {len(unique)} agencies (levels={levels_payload})"
    )
    return unique


# ── Orchestrator: fetch_and_parse 1 procedure ─────────────────────────────────

async def fetch_and_parse_procedure(
    client: httpx.AsyncClient,
    procedure_id_or_code: str,
) -> dict[str, Any] | None:
    """
    Fetch + parse 1 procedure end-to-end.

    Accept UUID (API id) hoặc procedure code ("1.001020"). Nếu là code, sẽ
    list-all với q=<code> để lấy UUID rồi mới detail.
    """
    s = (procedure_id_or_code or "").strip()
    if not s:
        return None

    # UUID-like (có dấu '-') → dùng thẳng. Code có dạng "1.001020".
    if "-" in s:
        api_id = s
    else:
        data = await list_procedures_page(client, "", limit=5, q=s)
        if not data or not data.get("items"):
            logger.warning(f"DVCQG | fetch_and_parse | code not found | code={s}")
            return None
        # Tìm exact match trước, rồi fallback first
        api_id = None
        for it in data["items"]:
            if (it.get("code") or "").strip() == s or (it.get("codeNotation") or "").strip() == s:
                api_id = it.get("id")
                break
        if not api_id:
            api_id = data["items"][0].get("id")
        if not api_id:
            return None

    detail = await get_procedure_detail(client, api_id)
    if not detail:
        return None

    try:
        parsed = parse_formality_json(detail)
    except Exception as e:
        logger.warning(f"DVCQG | parse failed | id={api_id} | {e}")
        return None

    if not parsed.get("code"):
        logger.warning(f"DVCQG | parsed missing code | id={api_id}")
        return None
    return parsed


async def fetch_procedures(
    items: Iterable[dict[str, Any]],
    *,
    concurrency: int | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any] | None]]:
    """
    Async generator: yields (code, parsed_or_None) cho từng item.

    `items` là list dict từ `discover_all_procedures` (mỗi item phải có 'id'
    và 'code'). Bounded concurrency qua semaphore. Dùng chung 1 client để
    tận dụng connection + cookie warmup.
    """
    conc = concurrency or settings.DVCQG_CRAWL_CONCURRENCY
    sem = asyncio.Semaphore(conc)
    items_list = list(items)

    async with httpx.AsyncClient(
        http2=False, follow_redirects=True, timeout=settings.CRAWLER_TIMEOUT
    ) as client:
        await _warmup(client)

        async def _one(item: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
            async with sem:
                api_id = item.get("id")
                code = (item.get("code") or item.get("codeNotation") or "").strip()
                if not api_id:
                    return code, None
                detail = await get_procedure_detail(client, api_id)
                if not detail:
                    return code, None
                try:
                    parsed = parse_formality_json(detail)
                except Exception as e:
                    logger.warning(f"DVCQG | parse failed | code={code} | {e}")
                    return code, None
                return parsed.get("code") or code, parsed

        tasks = [asyncio.create_task(_one(it)) for it in items_list]
        for fut in asyncio.as_completed(tasks):
            yield await fut


# ── Download biểu mẫu (utility, dùng cho test/debug) ──────────────────────────

async def download_attachment(
    client: httpx.AsyncClient,
    file_id: str,
) -> bytes | None:
    """
    POST preview-attachment với body {fileId} → bytes của file.

    LƯU Ý: dù URL trông như GET endpoint, server actually từ chối GET
    (trả "Request Rejected" HTML). Phải dùng POST. Server trả 201 cho
    thành công (không phải 200).
    """
    try:
        r = await client.post(
            ATTACHMENT_URL,
            json={"fileId": file_id},
            headers={**HEADERS, "Accept": "*/*"},
            timeout=settings.CRAWLER_TIMEOUT * 2,
        )
        if r.status_code not in (200, 201):
            logger.warning(
                f"DVCQG | download attachment | status={r.status_code} | fileId={file_id}"
            )
            return None
        # Sanity check: nếu content-type là text/html thì là page Request Rejected
        ctype = (r.headers.get("content-type") or "").lower()
        if "text/html" in ctype:
            logger.warning(
                f"DVCQG | download attachment rejected (html response) | fileId={file_id}"
            )
            return None
        return r.content
    except Exception as e:
        logger.warning(f"DVCQG | download attachment failed | fileId={file_id} | {e}")
        return None
