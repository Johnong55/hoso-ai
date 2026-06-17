# app/worker/tasks.py
"""
Celery tasks cho crawl + embed thủ tục hành chính.

Flow (JSON API mới của dichvucong.gov.vn):
  DocumentSource.source_url quy định phạm vi crawl:
    - "" hoặc "all"  → toàn bộ thủ tục (paginate hết list-all-formality)
    - "<departmentCode>" (vd "G19", "D01")  → 1 bộ/ngành, filter server-side

  Pipeline mỗi task run:
    1. discover_all_procedures → list item (id, code, ...) từ list-all API
    2. Với mỗi item: get detail → parse → upsert DB
    3. Chunking + embed sang Qdrant
    4. Change detection: skip embed nếu procedure.source_updated_at == API.updatedAt
"""
import asyncio
from datetime import datetime, timezone

from loguru import logger

from app.worker.celery_app import celery_app


def _run_async(coro):
    """
    Run an async coroutine from a sync Celery task.

    Mỗi task tạo event loop riêng. Trước khi đóng loop, dispose engine
    để xóa sạch connection pool — tránh lỗi 'Event loop is closed' ở task kế tiếp
    khi pool cố reuse connection từ loop cũ.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            from app.db.base import engine
            loop.run_until_complete(engine.dispose())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)


@celery_app.task(
    name="app.worker.tasks.crawl_and_embed_procedure",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def crawl_and_embed_procedure(self, source_id: str) -> dict:
    """Discover thủ tục qua list-all → tải detail JSON → parse → upsert → embed."""
    return _run_async(_crawl_and_embed_async(self, source_id))


async def _crawl_and_embed_async(task, source_id: str) -> dict:
    from sqlalchemy import select

    from app.core.config import settings
    from app.db.base import AsyncSessionLocal
    from app.models.document import (
        CrawlDiffLog,
        CrawlStatus,
        DocumentChunk,
        DocumentSource,
        ProcessingStatus,
    )
    from app.crawler.sources.dvcqg_json import (
        discover_all_procedures,
        discover_procedures_for_province,
        fetch_procedures,
    )

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(DocumentSource).where(DocumentSource.id == source_id))
        source: DocumentSource | None = result.scalar_one_or_none()

        if not source or not source.is_active:
            logger.warning(f"Task | crawl | source not found or inactive | source_id={source_id}")
            return {"status": "skipped", "reason": "source_not_found"}

        source.crawl_status = CrawlStatus.CRAWLING
        await db.commit()

        # Snapshot procedure codes hiện đang gắn với source này — dùng để diff
        # với danh sách sau khi crawl (added/updated/removed).
        prev_codes_result = await db.execute(
            select(DocumentChunk.procedure_code)
            .where(
                DocumentChunk.source_id == source_id,
                DocumentChunk.procedure_code.is_not(None),
            )
            .distinct()
        )
        prev_codes: set[str] = {r[0] for r in prev_codes_result.all() if r[0]}

        try:
            # source_url:
            #   ""/"all"           → discover toàn bộ
            #   "<departmentCode>" → 1 bộ/ngành (G01, G19, ...), filter server-side
            #   "<provinceCode>"   → 1 tỉnh (H49, H50, ...) — phân biệt qua source_type
            scope = (source.source_url or "").strip()
            scope_low = scope.lower()
            scope_type = (source.source_type or "").strip()

            import httpx
            from app.crawler.sources.dvcqg_json import _warmup

            async with httpx.AsyncClient(
                http2=False, follow_redirects=True, timeout=settings.CRAWLER_TIMEOUT
            ) as client:
                await _warmup(client)

                if scope_low in ("", "all"):
                    items = await discover_all_procedures(client)
                    logger.info(f"Task | scope=ALL | items={len(items)}")
                elif scope_type == "dvcqg_province":
                    # Phase 12: crawl 1 tỉnh — dùng endpoint list-all-public với type=PROVINCE
                    items = await discover_procedures_for_province(
                        client, province_code=scope
                    )
                    logger.info(
                        f"Task | scope=PROVINCE code={scope} | items={len(items)}"
                    )
                else:
                    items = await discover_all_procedures(
                        client, department_code=scope
                    )
                    logger.info(
                        f"Task | scope=AGENCY code={scope} | items={len(items)}"
                    )

            if not items:
                # Crawl chạy OK nhưng nguồn rỗng (vd UBND tỉnh không có thủ tục
                # nào còn hiệu lực). Đây KHÔNG phải lỗi → set SKIPPED và return
                # luôn, tránh kích hoạt retry loop của Celery.
                source.last_crawled_at = datetime.now(timezone.utc)
                source.crawl_status = CrawlStatus.SKIPPED
                source.processing_status = ProcessingStatus.EMBEDDED
                source.error_message = (
                    f"Không có thủ tục nào trong nguồn (scope='{scope or 'all'}')."
                )
                await db.commit()
                logger.info(
                    f"Task | crawl | empty source | source_id={source_id} | scope={scope}"
                )
                return {
                    "status": "empty",
                    "items": 0,
                    "ok": 0,
                    "skipped": 0,
                    "failed": 0,
                    "chunks": 0,
                }

            # Track codes thấy trong lần crawl này + codes thực sự re-embed
            seen_codes: set[str] = set()
            embedded_codes: set[str] = set()

            # ── Fetch + parse + save từng procedure ───────────────────────────
            total_chunks = 0
            ok = 0
            skipped = 0       # procedures không đổi → không embed lại
            failed = 0
            concurrency = settings.DVCQG_CRAWL_CONCURRENCY

            async for code, parsed in fetch_procedures(items, concurrency=concurrency):
                if code:
                    seen_codes.add(code)
                if not parsed:
                    failed += 1
                    continue
                try:
                    n = await _process_parsed_procedure(
                        source_id=source_id,
                        parsed=parsed,
                    )
                    if n == SKIPPED_UNCHANGED:
                        skipped += 1
                    else:
                        total_chunks += n
                        ok += 1
                        if code:
                            embedded_codes.add(code)
                except Exception as e:
                    logger.warning(f"Task | persist failed | code={code} | {e}")
                    failed += 1

            # ── Diff bảng cũ vs mới ───────────────────────────────────────────
            added = embedded_codes - prev_codes          # NEW + được embed thành công
            updated = embedded_codes & prev_codes        # CŨ + được re-embed (content đổi)
            removed = prev_codes - seen_codes            # CŨ + không còn trong nguồn

            diff_log = CrawlDiffLog(
                source_id=source_id,
                added_count=len(added),
                updated_count=len(updated),
                removed_count=len(removed),
                total_after=len(seen_codes),
                # Cap 200 mỗi list để JSON không phình to
                added_codes=sorted(added)[:200],
                updated_codes=sorted(updated)[:200],
                removed_codes=sorted(removed)[:200],
            )
            db.add(diff_log)

            # ── Cập nhật trạng thái source ────────────────────────────────────
            source.last_crawled_at = datetime.now(timezone.utc)
            source.crawl_status = CrawlStatus.SUCCESS
            source.processing_status = ProcessingStatus.EMBEDDED
            source.error_message = None
            await db.commit()

            logger.info(
                f"Task | crawl_diff | source_id={source_id} | "
                f"added={len(added)} | updated={len(updated)} | removed={len(removed)}"
            )

            logger.info(
                f"Task | crawl | done | source_id={source_id} "
                f"| items={len(items)} | ok={ok} | skipped={skipped} "
                f"| failed={failed} | chunks={total_chunks}"
            )
            return {
                "status": "success",
                "items": len(items),
                "ok": ok,
                "skipped": skipped,
                "failed": failed,
                "chunks": total_chunks,
            }

        except Exception as exc:
            source.crawl_status = CrawlStatus.FAILED
            source.error_message = str(exc)[:1000]
            await db.commit()
            logger.error(f"Task | crawl | failed | source_id={source_id} | error={exc}")
            raise task.retry(exc=exc)


SKIPPED_UNCHANGED = -1  # sentinel: procedure không đổi → skip embed


async def _process_parsed_procedure(source_id: str, parsed: dict) -> int:
    """
    Nhận parsed dict từ dvcqg_json_parser → upsert DB + chunk + embed.
    Dùng session riêng + commit ngay → không bị ảnh hưởng bởi procedure khác.

    Trả về:
      -1  → SKIPPED (nội dung không đổi so với lần crawl trước, không embed lại)
       0  → procedure không có chunks (trống)
      >0  → số chunks mới đã embed
    """
    from sqlalchemy import delete, func, select

    from app.core.config import settings
    from app.db.base import AsyncSessionLocal
    from app.models.document import (
        ChunkType, DocumentChunk, DocumentSource,
        EmbeddingStatus, ProcessingStatus,
    )
    from app.models.procedure import (
        AuthorityLevel, Procedure, ProcedureFee, ProcedureRequirement,
        ProcedureStatus, ProcedureStep,
    )
    from app.rag.chunking.strategy import ProcedureChunker
    from app.rag.embedding.embedder import Embedder

    async with AsyncSessionLocal() as db:
        try:
            # ── Upsert Procedure record ───────────────────────────────────────
            code = parsed.get("code")
            if not code:
                logger.warning("Task | missing code in parsed dict, skip")
                return 0

            api_updated_at = parsed.get("source_updated_at")  # epoch ms từ API

            existing = (await db.execute(
                select(Procedure).where(Procedure.code == code)
            )).scalar_one_or_none()

            # ── EARLY EXIT: API.updatedAt không đổi + chunks vẫn còn ─────────
            # source_updated_at là gốc thật ở phía server — chính xác hơn hash
            # nội dung, không bị ảnh hưởng bởi thay đổi format/cosmetic.
            if (
                existing
                and api_updated_at is not None
                and existing.source_updated_at == api_updated_at
            ):
                chunk_count = (await db.execute(
                    select(func.count(DocumentChunk.id)).where(
                        DocumentChunk.procedure_id == existing.id,
                        DocumentChunk.is_current == True,  # noqa: E712
                    )
                )).scalar() or 0
                if chunk_count > 0:
                    logger.info(
                        f"Task | UNCHANGED, skip embed | code={code} "
                        f"| existing_chunks={chunk_count} | updatedAt={api_updated_at}"
                    )
                    existing.status = ProcedureStatus.ACTIVE
                    await db.commit()
                    return SKIPPED_UNCHANGED

            # Truncate fields có giới hạn VARCHAR
            raw_name = parsed.get("name") or "Không rõ"
            safe_name = raw_name[:255]
            safe_agency = (parsed.get("implementing_agency") or "")[:255] or None
            safe_domain = (parsed.get("domain") or "")[:100] or None
            fee_summary = (parsed.get("fee_summary") or "")[:500] or None
            proc_time = (parsed.get("processing_time") or "")[:200] or None

            formality_id = (parsed.get("source_id") or "").strip() or None
            if existing:
                procedure = existing
                procedure.name = safe_name
                procedure.domain = safe_domain
                procedure.description = parsed.get("description")
                procedure.implementing_agency = safe_agency
                procedure.authority = safe_agency
                procedure.processing_time = proc_time
                procedure.fee = fee_summary
                procedure.result = parsed.get("result")
                procedure.legal_basis = parsed.get("legal_basis")
                procedure.status = ProcedureStatus.ACTIVE
                if formality_id:
                    procedure.formality_id = formality_id
            else:
                procedure = Procedure(
                    code=code,
                    name=safe_name,
                    domain=safe_domain,
                    implementing_agency=safe_agency,
                    authority=safe_agency,
                    processing_time=proc_time,
                    fee=fee_summary,
                    result=parsed.get("result"),
                    legal_basis=parsed.get("legal_basis"),
                    authority_level=AuthorityLevel.CENTRAL,
                    status=ProcedureStatus.ACTIVE,
                    formality_id=formality_id,
                )
                db.add(procedure)

            await db.flush()  # lấy procedure.id

            # ── Xóa requirements + steps + fees cũ trước khi insert mới ──────
            await db.execute(
                delete(ProcedureRequirement).where(ProcedureRequirement.procedure_id == procedure.id)
            )
            await db.execute(
                delete(ProcedureStep).where(ProcedureStep.procedure_id == procedure.id)
            )
            await db.execute(
                delete(ProcedureFee).where(ProcedureFee.procedure_id == procedure.id)
            )

            # ── Lưu fees mới ─────────────────────────────────────────────────
            for f in parsed.get("fees", []) or []:
                db.add(ProcedureFee(
                    procedure_id=procedure.id,
                    submission_method=(f.get("submission_method") or "")[:100],
                    processing_time=(f.get("processing_time") or "")[:200] or None,
                    amount_text=(f.get("amount_text") or "")[:300] or None,
                    description=f.get("description"),
                    order=f.get("order", 0),
                ))

            # ── Lưu requirements mới ─────────────────────────────────────────
            for i, req in enumerate(parsed.get("requirements", []) or []):
                db.add(ProcedureRequirement(
                    procedure_id=procedure.id,
                    name=(req.get("name") or "")[:255],
                    description=req.get("description"),
                    is_mandatory=req.get("is_mandatory", True),
                    order=i,
                    form_name=(req.get("form_name") or "")[:300] or None,
                    form_url=req.get("form_url"),
                    quantity=(req.get("quantity") or "")[:100] or None,
                    case_group=(req.get("case_group") or "")[:500] or None,
                ))

            # ── Lưu step blob (1 row/procedure theo yêu cầu) ────────────────
            steps_text = parsed.get("steps_text") or ""
            if steps_text:
                db.add(ProcedureStep(
                    procedure_id=procedure.id,
                    step_order=1,
                    title="Trình tự thực hiện",
                    description=steps_text,
                ))

            source_result = await db.execute(
                select(DocumentSource).where(DocumentSource.id == source_id)
            )
            source = source_result.scalar_one()

            # Mark old chunks of this procedure as stale + delete vectors
            old_chunks_result = await db.execute(
                select(DocumentChunk).where(
                    DocumentChunk.source_id == source_id,
                    DocumentChunk.procedure_id == procedure.id,
                    DocumentChunk.is_current == True,  # noqa: E712
                )
            )
            old_chunks = old_chunks_result.scalars().all()
            old_vector_ids = [c.vector_id for c in old_chunks if c.vector_id]

            embedder = Embedder()
            if old_vector_ids:
                try:
                    embedder.delete_by_ids(old_vector_ids)
                except Exception as e:
                    logger.warning(f"Task | delete old vectors failed | {e}")

            for c in old_chunks:
                c.is_current = False

            # ── Chunk + embed ─────────────────────────────────────────────────
            # Inject id để chunking ghi vào payload Qdrant (cho debug/filter)
            parsed["id"] = procedure.id
            parsed["authority_level"] = AuthorityLevel.CENTRAL.value

            chunker = ProcedureChunker()
            chunks = chunker.chunk_procedure(parsed)

            if not chunks:
                logger.warning(f"Task | no chunks generated | code={code}")
                await db.commit()
                return 0

            embedded = embedder.embed_chunks(chunks, source_id)

            for idx, item in enumerate(embedded):
                db.add(DocumentChunk(
                    source_id=source_id,
                    procedure_id=procedure.id,
                    vector_id=item["vector_id"],
                    content=item["content"],
                    chunk_index=idx,
                    chunk_type=item.get("chunk_type", ChunkType.GENERAL),
                    procedure_code=code,
                    domain=safe_domain,
                    authority_level=AuthorityLevel.CENTRAL.value,
                    locality=item["metadata"].get("locality"),
                    section=(item["metadata"].get("section") or "")[:200],
                    step_order=item["metadata"].get("step_order"),
                    is_current=True,
                    embedding_model=settings.EMBEDDING_MODEL,
                    embedding_status=EmbeddingStatus.DONE,
                ))

            # Cập nhật source_updated_at để lần crawl sau skip nếu API không đổi
            if api_updated_at is not None:
                procedure.source_updated_at = api_updated_at

            source.processing_status = ProcessingStatus.EMBEDDED

            # ── Commit ngay cho procedure này ────────────────────────────────
            await db.commit()

            logger.info(f"Task | procedure done | code={code} | chunks={len(embedded)}")

            # Phase 11: parse file biểu mẫu trong background (fail-soft)
            try:
                parse_procedure_forms.delay(procedure.id)
            except Exception as e:
                logger.warning(f"Task | enqueue parse_forms failed | {e}")

            return len(embedded)

        except Exception as exc:
            await db.rollback()
            raise


# ── Crawl 1 thủ tục lẻ theo mã ─────────────────────────────────────────────────

MANUAL_SOURCE_URL = "manual:single-procedures"


async def _ensure_manual_source() -> str:
    """Lấy (hoặc tạo) 1 DocumentSource dùng chung cho các thủ tục crawl lẻ theo mã."""
    from sqlalchemy import select
    from app.db.base import AsyncSessionLocal
    from app.models.document import DocumentSource

    async with AsyncSessionLocal() as db:
        existing = (await db.execute(
            select(DocumentSource).where(DocumentSource.source_url == MANUAL_SOURCE_URL)
        )).scalar_one_or_none()
        if existing:
            return existing.id
        source = DocumentSource(
            title="Thủ tục lẻ (crawl theo mã)",
            source_url=MANUAL_SOURCE_URL,
            source_type="dvcqg_manual",
            is_active=True,
        )
        db.add(source)
        await db.flush()
        sid = source.id
        await db.commit()
        return sid


@celery_app.task(
    name="app.worker.tasks.crawl_single_procedure",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def crawl_single_procedure(self, code: str) -> dict:
    """Crawl + embed 1 thủ tục theo mã TTHC (vd '1.015028')."""
    return _run_async(_crawl_single_async(self, code))


async def _crawl_single_async(task, code: str) -> dict:
    import httpx
    from app.core.config import settings
    from app.crawler.sources.dvcqg_json import _warmup, fetch_and_parse_procedure

    source_id = await _ensure_manual_source()

    try:
        async with httpx.AsyncClient(
            http2=False, follow_redirects=True, timeout=settings.CRAWLER_TIMEOUT
        ) as client:
            await _warmup(client)
            parsed = await fetch_and_parse_procedure(client, code)

        if not parsed:
            logger.warning(f"Task | single | no data | code={code}")
            return {"status": "not_found", "code": code}

        n = await _process_parsed_procedure(source_id=source_id, parsed=parsed)
        logger.info(f"Task | single | done | code={code} | chunks={n}")
        return {"status": "success", "code": code, "chunks": n, "source_id": source_id}

    except Exception as exc:
        logger.error(f"Task | single | failed | code={code} | {exc}")
        raise task.retry(exc=exc)


@celery_app.task(name="app.worker.tasks.scheduled_crawl")
def scheduled_crawl() -> dict:
    """Nightly job: crawl all active sources."""
    return _run_async(_scheduled_crawl_async())


async def _scheduled_crawl_async() -> dict:
    """Trigger crawl các source có next_crawl_at <= now và frequency != manual.

    Sau khi enqueue, tự đẩy next_crawl_at theo crawl_frequency của source
    (daily: +1 ngày, weekly: +7 ngày, monthly: +30 ngày).
    """
    from datetime import timedelta
    from sqlalchemy import or_, select

    from app.db.base import AsyncSessionLocal
    from app.models.document import CrawlFrequency, DocumentSource

    now = datetime.now(timezone.utc)
    triggered = 0
    skipped_manual = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DocumentSource).where(
                DocumentSource.is_active == True,  # noqa: E712
                DocumentSource.crawl_frequency != CrawlFrequency.MANUAL,
                or_(
                    DocumentSource.next_crawl_at.is_(None),  # chưa lên lịch lần đầu
                    DocumentSource.next_crawl_at <= now,
                ),
            )
        )
        due_sources = result.scalars().all()

        for source in due_sources:
            freq = source.crawl_frequency
            if freq == CrawlFrequency.DAILY:
                source.next_crawl_at = now + timedelta(days=1)
            elif freq == CrawlFrequency.WEEKLY:
                source.next_crawl_at = now + timedelta(days=7)
            elif freq == CrawlFrequency.MONTHLY:
                source.next_crawl_at = now + timedelta(days=30)
            else:
                skipped_manual += 1
                continue

            crawl_and_embed_procedure.delay(source.id)
            triggered += 1

        await db.commit()

    logger.info(
        f"Task | scheduled_crawl | triggered={triggered} | skipped_manual={skipped_manual}"
    )
    return {"triggered": triggered, "skipped_manual": skipped_manual}


@celery_app.task(name="app.worker.tasks.retry_failed_embeddings")
def retry_failed_embeddings() -> dict:
    """Hourly: retry any chunks that failed embedding."""
    return _run_async(_retry_failed_async())


async def _retry_failed_async() -> dict:
    from sqlalchemy import select

    from app.db.base import AsyncSessionLocal
    from app.models.document import CrawlStatus, DocumentSource

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DocumentSource).where(DocumentSource.crawl_status == CrawlStatus.FAILED)
        )
        failed_sources = result.scalars().all()

    retried = 0
    for source in failed_sources:
        crawl_and_embed_procedure.delay(source.id)
        retried += 1

    logger.info(f"Task | retry_failed | retried={retried}")
    return {"retried": retried}


# ── Phase 11: parse file biểu mẫu cho hướng dẫn điền form ─────────────────────

_FORM_ID_PROXY_RE = None
_FORM_ID_LEGACY_RE = None


def _extract_file_id(form_url: str) -> str | None:
    """
    Trích `fileId` từ form_url để gọi `download_attachment()`. Hỗ trợ:
      - Proxy mới: /api/v1/forms/<uuid>?name=...
      - Legacy DVCQG: ?fileId= / ?file_id= / ?ma= / ?id=
    Trả None nếu không nhận dạng được — caller set status='unsupported'.
    """
    import re
    from urllib.parse import unquote

    global _FORM_ID_PROXY_RE, _FORM_ID_LEGACY_RE
    if _FORM_ID_PROXY_RE is None:
        _FORM_ID_PROXY_RE = re.compile(r"/api/v1/forms/([^/?#]+)")
        _FORM_ID_LEGACY_RE = re.compile(
            r"[?&](?:file_?id|ma|id)=([^&]+)", re.IGNORECASE
        )

    if not form_url:
        return None
    m = _FORM_ID_PROXY_RE.search(form_url)
    if m:
        return unquote(m.group(1))
    m = _FORM_ID_LEGACY_RE.search(form_url)
    if m:
        return unquote(m.group(1))
    return None


@celery_app.task(
    name="app.worker.tasks.parse_procedure_forms",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def parse_procedure_forms(self, procedure_id: str) -> dict:
    """
    Tải + parse tất cả file biểu mẫu của 1 procedure → lưu vào
    procedure_requirements.form_content_text + form_fields_json.

    Chạy sau khi crawl commit xong (fail-soft, không block crawl chính).
    Cũng dùng cho backfill data cũ qua script parse_existing_forms.py.
    """
    return _run_async(_parse_procedure_forms_async(procedure_id))


async def _parse_procedure_forms_async(procedure_id: str) -> dict:
    import asyncio
    from datetime import datetime, timezone

    import httpx
    from sqlalchemy import select

    from app.core.config import settings
    from app.crawler.parsers.form_parser import FormParser
    from app.crawler.sources.dvcqg_json import _warmup, download_attachment
    from app.db.base import AsyncSessionLocal
    from app.models.procedure import ProcedureRequirement

    async with AsyncSessionLocal() as db:
        reqs = (await db.execute(
            select(ProcedureRequirement).where(
                ProcedureRequirement.procedure_id == procedure_id,
                ProcedureRequirement.form_url.is_not(None),
            )
        )).scalars().all()

        if not reqs:
            return {"status": "skipped", "reason": "no_forms"}

        # Group theo form_url unique để không tải/parse cùng file nhiều lần
        by_url: dict[str, list[ProcedureRequirement]] = {}
        for r in reqs:
            if r.form_url:
                by_url.setdefault(r.form_url, []).append(r)

        parser = FormParser()
        now = datetime.now(timezone.utc)
        stats = {"ok": 0, "failed": 0, "unsupported": 0}

        async with httpx.AsyncClient(
            http2=False,
            follow_redirects=True,
            timeout=settings.CRAWLER_TIMEOUT * 2,
        ) as client:
            await _warmup(client)
            sem = asyncio.Semaphore(3)

            async def _one(url: str, group: list[ProcedureRequirement]):
                async with sem:
                    file_id = _extract_file_id(url)
                    form_name = next((r.form_name for r in group if r.form_name), "") or ""

                    if not file_id:
                        for r in group:
                            r.form_parse_status = "unsupported"
                            r.form_parsed_at = now
                        stats["unsupported"] += 1
                        return

                    content = await download_attachment(client, file_id)
                    if not content:
                        for r in group:
                            r.form_parse_status = "failed"
                            r.form_parsed_at = now
                        stats["failed"] += 1
                        return

                    parsed = parser.parse_bytes(
                        content=content,
                        form_name=form_name,
                        source_url=url,
                    )
                    status = (parsed or {}).get("status") or "failed"
                    text = (parsed or {}).get("raw_text") or None
                    fields = (parsed or {}).get("fields") or None

                    for r in group:
                        r.form_content_text = text
                        r.form_fields_json = fields
                        r.form_parse_status = status
                        r.form_parsed_at = now

                    if status not in stats:
                        stats[status] = 0
                    stats[status] += 1

            await asyncio.gather(*[
                _one(url, group) for url, group in by_url.items()
            ])

        await db.commit()

    logger.info(
        f"Task | parse_forms | proc={procedure_id} | urls={len(by_url)} | {stats}"
    )
    return {"status": "done", "procedure_id": procedure_id, **stats}


# ── Phase 9: pre-cache sections cho instant chip click ─────────────────────────

@celery_app.task(
    name="app.worker.tasks.prefetch_sections",
    bind=True,
    max_retries=1,
    default_retry_delay=10,
)
def prefetch_sections(
    self,
    session_id: str,
    procedure_code: str,
    section_types: list[str],
    user_context: str | None = None,
) -> dict:
    """
    Sau khi /chat/ask trả procedure_focus, enqueue task này để pre-generate
    nội dung TẤT CẢ section trong background. Lưu vào Redis 30 phút.

    Khi user click chip, /chat/section đọc Redis trước → instant. Nếu miss
    (race condition khi click sớm) → fall back live LLM.

    session_id giúp filter case_group theo user_context riêng của session.
    """
    return _run_async(_prefetch_async(session_id, procedure_code, section_types, user_context))


async def _prefetch_async(
    session_id: str,
    procedure_code: str,
    section_types: list[str],
    user_context: str | None,
) -> dict:
    import asyncio
    import time
    from sqlalchemy import select

    from app.db.base import AsyncSessionLocal
    from app.models.procedure import Procedure
    from app.services.chat.section_cache import set_section
    from app.services.chat.chat_service import ChatService
    from app.rag.generation.generator import Generator

    start = time.monotonic()
    async with AsyncSessionLocal() as db:
        proc = (await db.execute(
            select(Procedure).where(Procedure.code == procedure_code)
        )).scalar_one_or_none()
        if not proc:
            logger.warning(f"Prefetch | procedure not found | code={procedure_code}")
            return {"status": "not_found"}

        svc = ChatService(db)
        generator = Generator()

        # Build raw data + gen LLM cho từng section, song song
        async def _one(section_type: str) -> tuple[str, bool]:
            try:
                raw_data = await svc._build_section_raw_data(
                    section_type, proc, procedure_code
                )
                if not raw_data:
                    return section_type, False
                # forms data: chỉ section "forms" có
                forms_data: list[dict] = []
                if section_type == "forms":
                    from app.models.procedure import ProcedureRequirement
                    rows = (await db.execute(
                        select(
                            ProcedureRequirement.name,
                            ProcedureRequirement.form_name,
                            ProcedureRequirement.form_url,
                        ).where(
                            ProcedureRequirement.procedure_id == proc.id,
                            ProcedureRequirement.form_url.is_not(None),
                        )
                    )).all()
                    seen_urls: set[str] = set()
                    for name, fname, url in rows:
                        if not url or url in seen_urls:
                            continue
                        seen_urls.add(url)
                        forms_data.append({
                            "name": name,
                            "form_name": fname,
                            "url": url,
                            "procedure_code": proc.code,
                            "procedure_name": proc.name,
                        })

                # Gọi LLM (sync trong async wrapper) — generate_section là sync
                gen = generator.generate_section(
                    section_type=section_type,
                    procedure_name=proc.name,
                    procedure_code=proc.code,
                    raw_data=raw_data,
                    user_context=user_context,
                )
                await set_section(
                    session_id=session_id,
                    procedure_code=procedure_code,
                    section_type=section_type,
                    content=gen.answer,
                    forms=forms_data,
                )
                return section_type, True
            except Exception as e:
                logger.warning(
                    f"Prefetch | section {section_type} failed | {e}"
                )
                return section_type, False

        results = await asyncio.gather(*[_one(s) for s in section_types])

    elapsed = time.monotonic() - start
    ok_count = sum(1 for _, ok in results if ok)
    logger.info(
        f"Prefetch | code={procedure_code} | session={session_id[:8]}... "
        f"| sections={len(section_types)} ok={ok_count} | {elapsed:.1f}s"
    )
    return {
        "status": "done",
        "procedure_code": procedure_code,
        "sections_total": len(section_types),
        "sections_ok": ok_count,
        "elapsed_seconds": round(elapsed, 1),
    }
