# app/api/v1/endpoints/admin/stats.py
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db, require_admin
from app.models.conversation import (
    ConversationSession,
    Message,
    MessageRole,
    RAGGenerationLog,
    RAGQuery,
)
from app.models.document import DocumentChunk
from app.models.feedback import Feedback
from app.models.procedure import Procedure, ProcedureRequirement
from app.models.user import User
from app.schemas.admin import (
    DailyActivityItem,
    DomainCountItem,
    RAGStatsResponse,
    TopProcedureItem,
)

router = APIRouter(prefix="/stats", tags=["Admin - Stats"])


@router.get("", response_model=RAGStatsResponse)
async def get_rag_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    # ── Counters tổng ─────────────────────────────────────────────────────────
    total_procedures = (
        await db.execute(select(func.count()).select_from(Procedure))
    ).scalar_one()
    total_chunks = (
        await db.execute(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.is_current == True)  # noqa: E712
        )
    ).scalar_one()
    total_sessions = (
        await db.execute(select(func.count()).select_from(ConversationSession))
    ).scalar_one()
    total_queries = (
        await db.execute(select(func.count()).select_from(RAGQuery))
    ).scalar_one()
    total_users = (
        await db.execute(select(func.count()).select_from(User))
    ).scalar_one()
    total_forms_ok = (
        await db.execute(
            select(func.count())
            .select_from(ProcedureRequirement)
            .where(ProcedureRequirement.form_parse_status == "ok")
        )
    ).scalar_one()
    total_feedback = (
        await db.execute(select(func.count()).select_from(Feedback))
    ).scalar_one()

    # ── Chất lượng ────────────────────────────────────────────────────────────
    avg_latency = (
        await db.execute(select(func.avg(RAGGenerationLog.response_time)))
    ).scalar_one() or 0.0
    fallback_count = (
        await db.execute(
            select(func.count())
            .select_from(RAGGenerationLog)
            .where(RAGGenerationLog.is_fallback == True)  # noqa: E712
        )
    ).scalar_one()
    fallback_rate = (fallback_count / total_queries) if total_queries > 0 else 0.0
    avg_rating = (
        await db.execute(
            select(func.avg(Feedback.rating)).where(Feedback.rating.is_not(None))
        )
    ).scalar_one() or 0.0

    # ── Daily activity 7 ngày qua ─────────────────────────────────────────────
    today = date.today()
    start_day = today - timedelta(days=6)
    # Sessions mới mỗi ngày
    sessions_per_day_rows = (
        await db.execute(
            select(
                func.date(ConversationSession.created_at).label("d"),
                func.count().label("c"),
            )
            .where(func.date(ConversationSession.created_at) >= start_day)
            .group_by(func.date(ConversationSession.created_at))
        )
    ).all()
    sessions_by_day = {str(r.d): r.c for r in sessions_per_day_rows}
    # Queries (user messages) mỗi ngày
    queries_per_day_rows = (
        await db.execute(
            select(
                func.date(Message.created_at).label("d"),
                func.count().label("c"),
            )
            .where(
                func.date(Message.created_at) >= start_day,
                Message.role == MessageRole.USER,
            )
            .group_by(func.date(Message.created_at))
        )
    ).all()
    queries_by_day = {str(r.d): r.c for r in queries_per_day_rows}

    daily_activity: list[DailyActivityItem] = []
    for i in range(7):
        d = start_day + timedelta(days=i)
        key = d.isoformat()
        daily_activity.append(
            DailyActivityItem(
                date=key,
                sessions=int(sessions_by_day.get(key, 0)),
                queries=int(queries_by_day.get(key, 0)),
            )
        )

    # ── Phân bố theo lĩnh vực (top 10 domain) ────────────────────────────────
    domain_rows = (
        await db.execute(
            select(
                Procedure.domain,
                func.count().label("c"),
            )
            .where(Procedure.domain.is_not(None))
            .group_by(Procedure.domain)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()
    domain_distribution = [
        DomainCountItem(
            domain=(r.domain or "Khác")[:50],  # truncate cho gọn chart label
            count=int(r.c),
        )
        for r in domain_rows
    ]

    # ── Top 5 procedures được hỏi nhiều nhất ──────────────────────────────────
    # Chain: RAGGenerationLog → RAGQuery → RAGRetrieval → DocumentChunk.procedure_code
    # Đơn giản hơn: dùng audit chain qua DocumentChunk được retrieve nhiều nhất
    from app.models.conversation import RAGRetrieval

    top_proc_rows = (
        await db.execute(
            select(
                DocumentChunk.procedure_code,
                func.count(RAGRetrieval.id).label("cnt"),
            )
            .join(RAGRetrieval, RAGRetrieval.chunk_id == DocumentChunk.id)
            .where(DocumentChunk.procedure_code.is_not(None))
            .group_by(DocumentChunk.procedure_code)
            .order_by(func.count(RAGRetrieval.id).desc())
            .limit(5)
        )
    ).all()
    top_codes = [r.procedure_code for r in top_proc_rows]
    top_count_by_code = {r.procedure_code: int(r.cnt) for r in top_proc_rows}
    top_procedures: list[TopProcedureItem] = []
    if top_codes:
        name_rows = (
            await db.execute(
                select(Procedure.code, Procedure.name).where(Procedure.code.in_(top_codes))
            )
        ).all()
        names = {r.code: r.name for r in name_rows}
        for code in top_codes:
            top_procedures.append(
                TopProcedureItem(
                    code=code,
                    name=names.get(code, "(Không tìm thấy)")[:80],
                    count=top_count_by_code.get(code, 0),
                )
            )

    # ── Top 5 procedures bị rate thấp (avg rating < 3, count >= 1) ─────────────
    # Chain feedback → message → RAG audit → chunk.procedure_code
    low_rated_rows = (
        await db.execute(
            select(
                DocumentChunk.procedure_code,
                func.avg(Feedback.rating).label("avg_r"),
                func.count(Feedback.id).label("cnt"),
            )
            .join(RAGGenerationLog, RAGGenerationLog.message_id == Feedback.message_id)
            .join(RAGQuery, RAGGenerationLog.rag_query_id == RAGQuery.id)
            .join(RAGRetrieval, RAGRetrieval.query_id == RAGQuery.id)
            .join(DocumentChunk, RAGRetrieval.chunk_id == DocumentChunk.id)
            .where(
                Feedback.rating.is_not(None),
                DocumentChunk.procedure_code.is_not(None),
            )
            .group_by(DocumentChunk.procedure_code)
            .having(func.avg(Feedback.rating) < 3.0)
            .order_by(func.avg(Feedback.rating).asc())
            .limit(5)
        )
    ).all()
    low_codes = [r.procedure_code for r in low_rated_rows]
    top_low_rated: list[TopProcedureItem] = []
    if low_codes:
        name_rows = (
            await db.execute(
                select(Procedure.code, Procedure.name).where(Procedure.code.in_(low_codes))
            )
        ).all()
        names = {r.code: r.name for r in name_rows}
        for r in low_rated_rows:
            top_low_rated.append(
                TopProcedureItem(
                    code=r.procedure_code,
                    name=names.get(r.procedure_code, "(Không tìm thấy)")[:80],
                    count=int(r.cnt),
                    avg_rating=round(float(r.avg_r), 2),
                )
            )

    return RAGStatsResponse(
        total_procedures=total_procedures,
        total_chunks=total_chunks,
        total_sessions=total_sessions,
        total_queries=total_queries,
        total_users=total_users,
        total_forms_ok=total_forms_ok,
        total_feedback=total_feedback,
        avg_latency_ms=round(float(avg_latency) * 1000, 2),
        fallback_rate=round(fallback_rate, 4),
        avg_score=0.0,
        avg_rating=round(float(avg_rating), 2),
        daily_activity=daily_activity,
        domain_distribution=domain_distribution,
        top_procedures=top_procedures,
        top_low_rated=top_low_rated,
    )
