"""
Redis cache cho section content đã được LLM generate.

Mục tiêu:
  - Sau /chat/ask, Celery task pre-fetch song song 5 section của procedure.
  - User click chip → /chat/section check Redis trước → hit thì return ~50ms,
    không cần gọi LLM lần nữa.
  - Miss (race condition khi user click trước khi pre-fetch xong) → fall back
    live LLM + lưu cache cho lần sau.

Key strategy:
  section_cache:{session_id}:{procedure_code}:{section_type}

  Có session_id để mỗi user thấy filtered theo tình huống của họ (user_context
  khác nhau → kết quả khác nhau). Nếu muốn share giữa user (tốn ít LLM hơn),
  có thể bỏ session_id sau khi grouping theo user_context hash.

TTL: 30 phút (1800s). Đủ cho session demo + cho phép data DB đổi (re-crawl)
mỗi nửa giờ tự refresh.
"""
from __future__ import annotations

import json

import redis.asyncio as aioredis
from loguru import logger

from app.core.config import settings


# Singleton client — tránh tạo connection mỗi request
_client: aioredis.Redis | None = None


def _get_client() -> aioredis.Redis:
    global _client
    if _client is None:
        # decode_responses=True để get() trả str, không phải bytes
        _client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


# Section cache TTL 30 phút.
SECTION_CACHE_TTL_SECONDS = 30 * 60


def _key(session_id: str, procedure_code: str, section_type: str) -> str:
    return f"section_cache:{session_id}:{procedure_code}:{section_type}"


async def get_section(
    session_id: str,
    procedure_code: str,
    section_type: str,
) -> dict | None:
    """
    Trả về dict {content, forms} hoặc None nếu chưa có cache.

    forms là list[dict] (serialized FormItem) — caller convert lại Pydantic.
    """
    if not session_id or not procedure_code or not section_type:
        return None
    try:
        client = _get_client()
        raw = await client.get(_key(session_id, procedure_code, section_type))
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"SectionCache | get failed | {e}")
        return None


async def set_section(
    session_id: str,
    procedure_code: str,
    section_type: str,
    content: str,
    forms: list[dict] | None = None,
    ttl: int = SECTION_CACHE_TTL_SECONDS,
) -> bool:
    """Lưu section content + forms vào Redis với TTL. Trả True nếu OK."""
    if not session_id or not procedure_code or not section_type:
        return False
    try:
        payload = json.dumps({"content": content, "forms": forms or []})
        client = _get_client()
        await client.setex(_key(session_id, procedure_code, section_type), ttl, payload)
        return True
    except Exception as e:
        logger.warning(f"SectionCache | set failed | {e}")
        return False


async def invalidate_procedure(session_id: str, procedure_code: str) -> int:
    """
    Xoá tất cả section cache của 1 procedure trong session. Trả số keys đã xoá.
    Dùng khi user explicitly reset hoặc khi data DB của procedure đổi.
    """
    try:
        client = _get_client()
        pattern = f"section_cache:{session_id}:{procedure_code}:*"
        # SCAN để không block Redis (KEYS chậm với data lớn)
        deleted = 0
        async for key in client.scan_iter(match=pattern, count=100):
            await client.delete(key)
            deleted += 1
        return deleted
    except Exception as e:
        logger.warning(f"SectionCache | invalidate failed | {e}")
        return 0


async def get_status(
    session_id: str,
    procedure_code: str,
    sections: list[str],
) -> dict[str, bool]:
    """
    Check sections nào đã có cache.

    Trả: {"steps": True, "requirements": False, ...}
    Dùng cho endpoint /chat/section/status để FE poll/hiển thị icon.
    """
    if not session_id or not procedure_code:
        return {s: False for s in sections}
    try:
        client = _get_client()
        keys = [_key(session_id, procedure_code, s) for s in sections]
        if not keys:
            return {}
        # MGET trả None cho key chưa có
        values = await client.mget(keys)
        return {s: v is not None for s, v in zip(sections, values)}
    except Exception as e:
        logger.warning(f"SectionCache | get_status failed | {e}")
        return {s: False for s in sections}
