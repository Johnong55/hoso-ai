# app/services/chat/chat_service.py
import math

from fastapi import HTTPException, status
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import (
    ConversationSession,
    Message,
    MessageRole,
    RAGGenerationLog,
    RAGQuery,
    RAGRetrieval,
)
from app.models.user import User
from app.rag.pipeline import RAGPipeline
from app.rag.retrieval.retriever import RetrievedChunk
from app.schemas.chat import (
    AskRequest,
    AskResponse,
    CreateSessionRequest,
    MessageResponse,
    SessionHistoryResponse,
    SessionResponse,
    SourceItem,
)
from app.schemas.common import PaginatedResponse

_pipeline = RAGPipeline()


class ChatService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Session management ────────────────────────────────────────────────────

    async def create_session(
        self,
        payload: CreateSessionRequest,
        user: User | None,
    ) -> SessionResponse:
        session = ConversationSession(
            user_id=user.id if user else None,
            is_guest=user is None,
            locality_filter=payload.locality,
            domain_filter=payload.domain,
        )
        self._db.add(session)
        await self._db.flush()
        logger.info(f"Chat | create_session | session_id={session.id} | user_id={session.user_id}")
        return SessionResponse.model_validate(session)

    async def get_session(self, session_id: str, user: User | None) -> ConversationSession:
        result = await self._db.execute(
            select(ConversationSession).where(ConversationSession.id == session_id)
        )
        session = result.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Phiên hội thoại không tồn tại.")

        # Guests can only access their own guest sessions within the same request context
        if user and session.user_id and session.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Không có quyền truy cập phiên này.")

        return session

    async def list_user_sessions(
        self,
        user: User,
        page: int = 1,
        page_size: int = 20,
    ) -> PaginatedResponse[SessionResponse]:
        count_result = await self._db.execute(
            select(func.count()).where(
                ConversationSession.user_id == user.id,
                ConversationSession.is_active == True,
            )
        )
        total = count_result.scalar_one()

        offset = (page - 1) * page_size
        result = await self._db.execute(
            select(ConversationSession)
            .where(ConversationSession.user_id == user.id, ConversationSession.is_active == True)
            .order_by(ConversationSession.updated_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        sessions = result.scalars().all()

        return PaginatedResponse(
            items=[SessionResponse.model_validate(s) for s in sessions],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=math.ceil(total / page_size) if total else 0,
        )

    async def get_session_history(
        self,
        session_id: str,
        user: User | None,
    ) -> SessionHistoryResponse:
        session = await self.get_session(session_id, user)

        result = await self._db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at)
        )
        messages = result.scalars().all()

        return SessionHistoryResponse(
            session=SessionResponse.model_validate(session),
            messages=[MessageResponse.model_validate(m) for m in messages],
        )

    async def delete_session(self, session_id: str, user: User) -> None:
        session = await self.get_session(session_id, user)
        session.is_active = False
        logger.info(f"Chat | delete_session | session_id={session_id}")

    # ── Ask / RAG flow ────────────────────────────────────────────────────────

    async def ask(
        self,
        payload: AskRequest,
        user: User | None,
    ) -> AskResponse:
        # Resolve or create session
        if payload.session_id:
            session = await self.get_session(payload.session_id, user)
        else:
            session = ConversationSession(
                user_id=user.id if user else None,
                is_guest=user is None,
                locality_filter=payload.locality,
                domain_filter=payload.domain,
            )
            self._db.add(session)
            await self._db.flush()

        # Load conversation history for context (last 6 turns)
        history = await self._load_history(session.id, limit=6)

        # Save user message
        user_msg = Message(
            session_id=session.id,
            role=MessageRole.USER,
            content=payload.question,
        )
        self._db.add(user_msg)
        await self._db.flush()

        # Run RAG pipeline
        result = await _pipeline.run(
            query=payload.question,
            locality=payload.locality or session.locality_filter,
            domain=payload.domain or session.domain_filter,
            conversation_history=history,
        )

        # Save assistant message
        assistant_msg = Message(
            session_id=session.id,
            role=MessageRole.ASSISTANT,
            content=result.answer,
        )
        self._db.add(assistant_msg)
        await self._db.flush()

        # Persist RAG audit trail only for authenticated users
        if user:
            await self._persist_rag_audit(
                user_message=user_msg,
                assistant_message=assistant_msg,
                result=result,
                payload=payload,
                session=session,
            )

        # Auto-title session on first exchange
        if not session.title:
            session.title = payload.question[:100]

        sources = self._build_sources(result.chunks)
        logger.info(
            f"Chat | ask | session_id={session.id} | user_id={user.id if user else 'guest'} "
            f"| fallback={result.is_fallback} | latency={result.latency_ms}ms"
        )

        return AskResponse(
            answer=result.answer,
            session_id=session.id,
            message_id=assistant_msg.id,
            sources=sources,
            is_fallback=result.is_fallback,
            latency_ms=result.latency_ms,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _load_history(self, session_id: str, limit: int = 6) -> list[dict]:
        result = await self._db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        messages = list(reversed(result.scalars().all()))
        return [{"role": m.role.value, "content": m.content} for m in messages]

    async def _persist_rag_audit(self, user_message: Message, assistant_message: Message, result, payload: AskRequest, session) -> None:
        rag_query = RAGQuery(
            message_id=user_message.id,
            original_query=payload.question,
            rewritten_query=result.rewritten_query,
            locality_filter=payload.locality or session.locality_filter,
            domain_filter=payload.domain or session.domain_filter,
        )
        self._db.add(rag_query)
        await self._db.flush()

        for rank, chunk in enumerate(result.chunks, 1):
            # chunk.vector_id matches DocumentChunk.vector_id
            from sqlalchemy import select as sa_select
            from app.models.document import DocumentChunk
            chunk_result = await self._db.execute(
                sa_select(DocumentChunk).where(DocumentChunk.vector_id == chunk.vector_id)
            )
            doc_chunk = chunk_result.scalar_one_or_none()
            if doc_chunk:
                self._db.add(RAGRetrieval(
                    query_id=rag_query.id,         # DD: query_id (was: rag_query_id)
                    chunk_id=doc_chunk.id,
                    score=chunk.score,
                    rank_order=rank,               # DD: rank_order (was: rank)
                    retrieval_method="vector",
                ))

        gen = result.generation
        self._db.add(RAGGenerationLog(
            rag_query_id=rag_query.id,
            message_id=assistant_message.id,
            system_prompt="[see generator.py SYSTEM_PROMPT]",
            prompt=payload.question,               # DD: prompt (was: full_prompt)
            response=result.answer,
            is_fallback=result.is_fallback,
            model=gen.model,
            prompt_tokens=gen.prompt_tokens,
            completion_tokens=gen.completion_tokens,
            total_tokens=gen.total_tokens,
            response_time=result.latency_ms / 1000,  # DD: response_time FLOAT (giây) — was: latency_ms INT
        ))

    def _build_sources(self, chunks: list[RetrievedChunk]) -> list[SourceItem]:
        sources = []
        for chunk in chunks:
            sources.append(SourceItem(
                chunk_id=chunk.vector_id,
                procedure_id=chunk.metadata.get("procedure_id") or None,
                procedure_code=chunk.metadata.get("procedure_code") or None,
                procedure_name=chunk.metadata.get("procedure_name") or None,
                chunk_type=chunk.metadata.get("chunk_type", ""),
                content_preview=chunk.content[:200],
                score=round(chunk.score, 4),
            ))
        return sources
