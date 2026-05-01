# app/api/v1/endpoints/admin/stats.py
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db, require_admin
from app.models.conversation import ConversationSession, RAGGenerationLog, RAGQuery
from app.models.document import DocumentChunk
from app.models.procedure import Procedure
from app.models.user import User
from app.schemas.admin import RAGStatsResponse

router = APIRouter(prefix="/stats", tags=["Admin - Stats"])


@router.get("", response_model=RAGStatsResponse)
async def get_rag_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    total_procedures = (await db.execute(select(func.count()).select_from(Procedure))).scalar_one()
    total_chunks = (
        await db.execute(
            select(func.count()).select_from(DocumentChunk).where(DocumentChunk.is_current == True)
        )
    ).scalar_one()
    total_sessions = (await db.execute(select(func.count()).select_from(ConversationSession))).scalar_one()
    total_queries = (await db.execute(select(func.count()).select_from(RAGQuery))).scalar_one()

    # response_time is stored in seconds (FLOAT); convert to ms for the API response
    avg_latency = (
        await db.execute(select(func.avg(RAGGenerationLog.response_time)))
    ).scalar_one() or 0.0

    fallback_count = (
        await db.execute(
            select(func.count()).select_from(RAGGenerationLog).where(RAGGenerationLog.is_fallback == True)
        )
    ).scalar_one()

    fallback_rate = (fallback_count / total_queries) if total_queries > 0 else 0.0

    return RAGStatsResponse(
        total_procedures=total_procedures,
        total_chunks=total_chunks,
        total_sessions=total_sessions,
        total_queries=total_queries,
        avg_latency_ms=round(float(avg_latency) * 1000, 2),  # seconds → ms
        fallback_rate=round(fallback_rate, 4),
        avg_score=0.0,  # populated from Chroma in future iteration
    )
