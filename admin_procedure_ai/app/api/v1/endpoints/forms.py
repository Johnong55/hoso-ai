# app/api/v1/endpoints/forms.py
"""
Public proxy để tải biểu mẫu từ DVCQG.

DVCQG endpoint `/api/v1/submitting/preview-attachment` chỉ chấp nhận POST với
body `{fileId}`. Browser click link sẽ GET → server trả "Request Rejected" HTML.

→ Endpoint này nhận GET (link bình thường trong UI), POST hộ sang DVCQG, rồi
stream bytes về cho browser với Content-Type + Content-Disposition đúng.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Query, Response, status
from loguru import logger

from app.core.config import settings
from app.crawler.sources.dvcqg_json import _warmup, download_attachment


router = APIRouter(prefix="/forms", tags=["Forms"])


def _detect_media_type(content: bytes, filename: str | None) -> str:
    """Đoán Content-Type từ magic bytes (ưu tiên) hoặc đuôi file."""
    if len(content) >= 4:
        if content[:4] == b"%PDF":
            return "application/pdf"
        if content[:4] == b"PK\x03\x04":
            # docx, xlsx, pptx, etc. đều là zip — heuristic theo đuôi
            if filename:
                ext = filename.lower().rsplit(".", 1)[-1]
                if ext == "docx":
                    return (
                        "application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document"
                    )
                if ext == "xlsx":
                    return (
                        "application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet"
                    )
            return "application/zip"
        if content[:4] == b"\xd0\xcf\x11\xe0":
            # OLE CFB — legacy .doc / .xls
            if filename and filename.lower().endswith(".xls"):
                return "application/vnd.ms-excel"
            return "application/msword"
    return "application/octet-stream"


@router.get("/{file_id}")
async def download_form(
    file_id: str,
    name: str | None = Query(
        None,
        description="Tên file gốc để browser save đúng (vd: 'TK dang ky khai sinh.doc')",
    ),
):
    """
    Proxy GET → POST sang DVCQG để tải biểu mẫu.

    Public (không cần auth) vì biểu mẫu là tài liệu công khai. Nếu muốn hạn chế,
    thêm Depends(get_current_user_optional) sau.
    """
    file_id = file_id.strip()
    if not file_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Thiếu file_id")

    async with httpx.AsyncClient(
        http2=False, follow_redirects=True, timeout=settings.CRAWLER_TIMEOUT * 2
    ) as client:
        await _warmup(client)
        content = await download_attachment(client, file_id)

    if not content:
        logger.warning(f"Forms | download failed | file_id={file_id}")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Không tải được biểu mẫu từ Cổng DVCQG.",
        )

    filename = (name or f"bieu-mau-{file_id}").strip() or f"bieu-mau-{file_id}"
    media_type = _detect_media_type(content, filename)
    return Response(
        content=content,
        media_type=media_type,
        headers={
            # RFC 5987: filename* cho non-ASCII (tên file tiếng Việt)
            "Content-Disposition": (
                f'attachment; filename="{filename}"; '
                f"filename*=UTF-8''{filename}"
            ),
            "Cache-Control": "public, max-age=3600",
        },
    )
