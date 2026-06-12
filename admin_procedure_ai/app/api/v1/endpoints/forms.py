# app/api/v1/endpoints/forms.py
"""
Public proxy để tải biểu mẫu từ DVCQG.

DVCQG endpoint `/api/v1/submitting/preview-attachment` chỉ chấp nhận POST với
body `{fileId}`. Browser click link sẽ GET → server trả "Request Rejected" HTML.

→ Endpoint này nhận GET (link bình thường trong UI), POST hộ sang DVCQG, rồi
stream bytes về cho browser với Content-Type + Content-Disposition đúng.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query, Response, status
from loguru import logger

from app.core.config import settings
from app.crawler.sources.dvcqg_json import _warmup, download_attachment


router = APIRouter(prefix="/forms", tags=["Forms"])

# Cache PDF đã convert sẵn cho preview. Disk persistence sống qua restart
# container nếu mount volume; mặc định /tmp → mất khi restart, không sao
# vì lần next request sẽ convert lại (mất ~3-5s).
_PREVIEW_CACHE_DIR = Path(os.environ.get("FORM_PREVIEW_CACHE_DIR", "/tmp/form_preview_cache"))
_PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# LibreOffice headless không thread-safe → serialize convert qua lock.
# Per-request convert mất ~3-5s, OK với queue 1 request/lần.
_LIBREOFFICE_LOCK = asyncio.Lock()


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


def _detect_ext(content: bytes, filename: str | None) -> str:
    """Suy đoán extension: pdf / docx / doc / xlsx / xls / other."""
    if filename:
        ext = filename.lower().rsplit(".", 1)[-1]
        if ext in {"pdf", "docx", "doc", "xlsx", "xls"}:
            return ext
    if len(content) >= 4:
        if content[:4] == b"%PDF":
            return "pdf"
        if content[:4] == b"PK\x03\x04":
            return "docx"  # heuristic — zip mà không đoán được thì coi như docx
        if content[:4] == b"\xd0\xcf\x11\xe0":
            return "doc"
    return "other"


async def _convert_to_pdf(content: bytes, ext: str) -> bytes | None:
    """Convert docx/doc bytes → PDF bytes qua LibreOffice headless.

    Trả None nếu LibreOffice không có hoặc convert fail. PDF input pass-through.
    """
    if ext == "pdf":
        return content
    if ext not in ("docx", "doc"):
        return None

    async with _LIBREOFFICE_LOCK:
        with tempfile.TemporaryDirectory(prefix="form_preview_") as tmpdir:
            tmp = Path(tmpdir)
            in_path = tmp / f"input.{ext}"
            in_path.write_bytes(content)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "soffice",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(tmp),
                    str(in_path),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                except asyncio.TimeoutError:
                    proc.kill()
                    logger.warning(f"Forms | LibreOffice convert timeout | ext={ext}")
                    return None
                if proc.returncode != 0:
                    logger.warning(
                        f"Forms | LibreOffice convert failed | rc={proc.returncode} "
                        f"| stderr={stderr.decode(errors='ignore')[:200]}"
                    )
                    return None
                pdf_path = tmp / "input.pdf"
                if not pdf_path.exists():
                    logger.warning("Forms | LibreOffice convert no output")
                    return None
                return pdf_path.read_bytes()
            except FileNotFoundError:
                logger.warning("Forms | soffice not found — install libreoffice in image")
                return None
            except Exception as e:
                logger.warning(f"Forms | LibreOffice convert error | {e}")
                return None


@router.get("/{file_id}/preview")
async def preview_form(
    file_id: str,
    name: str | None = Query(
        None,
        description="Tên file gốc (để render tab title cho phù hợp)",
    ),
):
    """Preview biểu mẫu inline (PDF native viewer).

    PDF input → trả thẳng. DOCX/DOC → convert qua LibreOffice headless,
    cache disk theo file_id. Reuse instant các lần sau.
    """
    file_id = file_id.strip()
    if not file_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Thiếu file_id")

    # Cache hit
    cache_path = _PREVIEW_CACHE_DIR / f"{file_id}.pdf"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        pdf_bytes = cache_path.read_bytes()
    else:
        # Tải bytes gốc từ DVCQG
        async with httpx.AsyncClient(
            http2=False, follow_redirects=True, timeout=settings.CRAWLER_TIMEOUT * 2
        ) as client:
            await _warmup(client)
            content = await download_attachment(client, file_id)
        if not content:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "Không tải được biểu mẫu từ Cổng DVCQG.",
            )

        ext = _detect_ext(content, name)
        if ext in {"xlsx", "xls", "other"}:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                f"Định dạng .{ext} chưa hỗ trợ xem trước, vui lòng tải về.",
            )

        pdf_bytes = await _convert_to_pdf(content, ext)
        if not pdf_bytes:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Không tạo được bản xem trước. Vui lòng tải về.",
            )

        # Cache best-effort, lỗi disk thì bỏ qua
        try:
            cache_path.write_bytes(pdf_bytes)
        except Exception as e:
            logger.warning(f"Forms | preview cache write failed | {e}")

    filename = (name or f"bieu-mau-{file_id}").strip() or f"bieu-mau-{file_id}"
    # Đổi đuôi → .pdf cho tab title phù hợp
    if "." in filename:
        filename = filename.rsplit(".", 1)[0] + ".pdf"
    else:
        filename = filename + ".pdf"
    ascii_filename = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    encoded_filename = quote(filename, safe="")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'inline; filename="{ascii_filename}"; '
                f"filename*=UTF-8''{encoded_filename}"
            ),
            "Cache-Control": "public, max-age=86400",  # 1 ngày
        },
    )


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
    # HTTP header phải ASCII. Tên file tiếng Việt (vd "7-Mẫu NA6.doc") cần:
    #   - filename="..."  → ASCII fallback (latin-1 thay non-ASCII bằng "_")
    #   - filename*=UTF-8''<percent-encoded>  → bản đầy đủ theo RFC 5987
    ascii_filename = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    encoded_filename = quote(filename, safe="")
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_filename}"; '
                f"filename*=UTF-8''{encoded_filename}"
            ),
            "Cache-Control": "public, max-age=3600",
        },
    )
