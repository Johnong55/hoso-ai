"""News endpoint — proxy DVCQG news API + cache Redis.

  - GET /news?limit=&last_id=&q=  → list (cursor pagination)
  - GET /news/{id}                → detail with HTML content

Public — biểu mẫu tin tức công khai, không cần auth.
Cache Redis 1h vì tin không đổi liên tục.
"""
from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel

from app.core.config import settings
from app.crawler.sources.dvcqg_news import (
    get_news_detail,
    list_news,
    make_client,
)


router = APIRouter(prefix="/news", tags=["News"])

_CACHE_TTL = 3600  # 1h
_client: aioredis.Redis | None = None


def _redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


async def _cache_get(key: str) -> Any | None:
    try:
        raw = await _redis().get(key)
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.warning(f"News cache get fail | {e}")
        return None


async def _cache_set(key: str, value: Any) -> None:
    try:
        await _redis().setex(key, _CACHE_TTL, json.dumps(value, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"News cache set fail | {e}")


class NewsItem(BaseModel):
    id: str
    title: str
    short_description: str | None = None
    category_id: str | None = None
    created_at: int | None = None
    updated_at: int | None = None
    order: int | None = None


class NewsListResponse(BaseModel):
    items: list[NewsItem]
    last_id: str | None = None
    total: int = 0


class NewsDetailResponse(NewsItem):
    content: str = ""


def _shape_item(row: dict[str, Any]) -> NewsItem:
    return NewsItem(
        id=row.get("id") or "",
        title=row.get("title") or "",
        short_description=row.get("shortDescription") or None,
        category_id=row.get("categoryId") or None,
        created_at=row.get("createdAt"),
        updated_at=row.get("updatedAt"),
        order=row.get("order"),
    )


@router.get("", response_model=NewsListResponse)
async def list_news_endpoint(
    limit: int = Query(9, ge=1, le=50),
    last_id: str = Query("", description="Cursor pagination — id cuối trang trước"),
    q: str = Query("", description="Tìm kiếm theo tiêu đề"),
):
    cache_key = f"news:list:{limit}:{last_id}:{q}"
    cached = await _cache_get(cache_key)
    if cached:
        return NewsListResponse(**cached)

    client = await make_client()
    try:
        data = await list_news(client, limit=limit, last_id=last_id, name=q)
    finally:
        await client.aclose()

    if data is None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Không tải được tin tức từ Cổng Dịch vụ công Quốc gia.",
        )

    rows = data.get("rows") or []
    response = NewsListResponse(
        items=[_shape_item(r) for r in rows],
        last_id=data.get("lastId") or None,
        total=data.get("total") or 0,
    )
    await _cache_set(cache_key, response.model_dump())
    return response


@router.get("/{news_id}", response_model=NewsDetailResponse)
async def get_news_detail_endpoint(news_id: str):
    news_id = news_id.strip()
    if not news_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Thiếu news_id")

    cache_key = f"news:detail:{news_id}"
    cached = await _cache_get(cache_key)
    if cached:
        return NewsDetailResponse(**cached)

    client = await make_client()
    try:
        data = await get_news_detail(client, news_id)
    finally:
        await client.aclose()

    if not data:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Không tìm thấy tin tức.",
        )

    item = _shape_item(data)
    response = NewsDetailResponse(
        **item.model_dump(),
        content=data.get("content") or "",
    )
    await _cache_set(cache_key, response.model_dump())
    return response
