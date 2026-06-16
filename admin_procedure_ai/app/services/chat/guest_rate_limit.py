"""
Rate limit cho khách vãng lai (guest) — giới hạn số câu hỏi /chat/ask theo IP.

Mục tiêu:
  - Tránh lạm dụng tài nguyên: 1 guest không thể spam vô hạn câu hỏi vì mỗi
    câu mất ~5s + tốn quota LLM của Cloudflare.
  - Khuyến khích đăng ký: hết quota → modal "Đăng ký để tiếp tục" hiển thị.

Cơ chế:
  Key Redis: `guest:rate:{ip}:{YYYY-MM-DD}` — counter INCR mỗi request.
  TTL = 24h, tự reset mỗi ngày.

Người dùng đã đăng nhập KHÔNG bị giới hạn ở đây (rate limit riêng cho user
account nếu cần sẽ làm ở guard khác).
"""
from __future__ import annotations

from datetime import datetime, timezone

import redis.asyncio as aioredis
from loguru import logger

from app.core.config import settings


# Hằng số cấu hình
GUEST_DAILY_LIMIT = 10               # số câu hỏi/ngày cho khách vãng lai
GUEST_RATE_TTL_SECONDS = 24 * 60 * 60  # 24h


_client: aioredis.Redis | None = None


def _get_client() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


def _today_key(ip: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"guest:rate:{ip}:{today}"


async def check_and_increment(ip: str) -> tuple[bool, int, int]:
    """
    Kiểm tra và tăng counter cho IP.

    Trả về (allowed, current_count, limit):
      - allowed=True nếu chưa vượt giới hạn (counter đã được tăng).
      - allowed=False nếu vượt (counter KHÔNG bị tăng tiếp).

    Nếu Redis lỗi → fail-open (cho qua, tránh chặn user oan).
    """
    if not ip:
        return True, 0, GUEST_DAILY_LIMIT

    try:
        client = _get_client()
        key = _today_key(ip)

        # Lấy giá trị hiện tại trước khi quyết định
        current = await client.get(key)
        current_int = int(current) if current else 0

        if current_int >= GUEST_DAILY_LIMIT:
            return False, current_int, GUEST_DAILY_LIMIT

        # INCR atomic — tránh race condition khi 2 request cùng lúc
        new_count = await client.incr(key)
        # Set TTL chỉ lần đầu (khi key mới tạo)
        if new_count == 1:
            await client.expire(key, GUEST_RATE_TTL_SECONDS)

        return True, new_count, GUEST_DAILY_LIMIT
    except Exception as e:
        logger.warning(f"GuestRateLimit | redis error | ip={ip} | {e}")
        # Fail-open: cho qua nếu Redis lỗi
        return True, 0, GUEST_DAILY_LIMIT


async def get_current_count(ip: str) -> int:
    """Trả về số câu hỏi guest đã hỏi trong ngày (không tăng counter)."""
    if not ip:
        return 0
    try:
        client = _get_client()
        current = await client.get(_today_key(ip))
        return int(current) if current else 0
    except Exception:
        return 0
