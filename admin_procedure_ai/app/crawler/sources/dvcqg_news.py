"""DVCQG news client — proxy 2 endpoint tin tức công khai.

  - POST /api/v1/configuring/news/list-by-citizen
      Body: {name, limit, lastId}
      Trả: rows (cursor pagination qua lastId)

  - POST /api/v1/configuring/news/detail-by-citizen
      Body: {id}
      Trả: chi tiết kèm `content` (HTML)

Public — không cần auth. Dùng warmup + retry chung với crawler hiện có.
"""
from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from app.core.config import settings
from app.crawler.sources.dvcqg_json import BASE, _post_with_retry, _warmup


NEWS_LIST_URL = f"{BASE}/api/v1/configuring/news/list-by-citizen"
NEWS_DETAIL_URL = f"{BASE}/api/v1/configuring/news/detail-by-citizen"


async def list_news(
    client: httpx.AsyncClient,
    *,
    limit: int = 9,
    last_id: str = "",
    name: str = "",
) -> dict[str, Any] | None:
    """Trả về dict {total, lastId, rows} hoặc None nếu fail."""
    r = await _post_with_retry(
        client,
        NEWS_LIST_URL,
        {"name": name, "limit": limit, "lastId": last_id},
        timeout=settings.CRAWLER_TIMEOUT,
    )
    if r is None:
        return None
    try:
        data = r.json()
        if data.get("code") != "OK":
            logger.warning(f"DVCQG news | list non-OK | code={data.get('code')}")
            return None
        return data.get("data") or {}
    except Exception as e:
        logger.warning(f"DVCQG news | list parse error | {e}")
        return None


async def get_news_detail(
    client: httpx.AsyncClient,
    news_id: str,
) -> dict[str, Any] | None:
    """Trả về dict đầy đủ (gồm `content` HTML) hoặc None nếu fail."""
    r = await _post_with_retry(
        client,
        NEWS_DETAIL_URL,
        {"id": news_id},
        timeout=settings.CRAWLER_TIMEOUT,
    )
    if r is None:
        return None
    try:
        data = r.json()
        if data.get("code") != "OK":
            logger.warning(f"DVCQG news | detail non-OK | code={data.get('code')}")
            return None
        return data.get("data") or None
    except Exception as e:
        logger.warning(f"DVCQG news | detail parse error | {e}")
        return None


async def make_client() -> httpx.AsyncClient:
    """httpx client với warmup đã chạy — caller close khi xong."""
    client = httpx.AsyncClient(
        http2=False, follow_redirects=True, timeout=settings.CRAWLER_TIMEOUT
    )
    await _warmup(client)
    return client
