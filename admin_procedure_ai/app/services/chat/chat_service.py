# app/services/chat/chat_service.py
import math
import re

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
    FormItem,
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

        # Re-derive forms cho ASSISTANT messages từ audit chain.
        # Cùng logic với _build_forms khi live: score gap nhỏ + filter theo mã
        # thủ tục được cite literal trong text answer.
        forms_by_msg: dict[str, list[FormItem]] = {}
        assistant_ids = [m.id for m in messages if m.role == MessageRole.ASSISTANT]
        msg_content_by_id = {m.id: m.content for m in messages if m.role == MessageRole.ASSISTANT}
        if assistant_ids:
            from app.models.document import DocumentChunk
            from app.models.procedure import Procedure, ProcedureRequirement

            # Pha 1: lấy (message_id, procedure_code, best_score) cho mỗi msg
            score_rows = (await self._db.execute(
                select(
                    RAGGenerationLog.message_id,
                    DocumentChunk.procedure_code,
                    func.max(RAGRetrieval.score).label("best_score"),
                )
                .join(RAGQuery, RAGGenerationLog.rag_query_id == RAGQuery.id)
                .join(RAGRetrieval, RAGRetrieval.query_id == RAGQuery.id)
                .join(DocumentChunk, RAGRetrieval.chunk_id == DocumentChunk.id)
                .where(
                    RAGGenerationLog.message_id.in_(assistant_ids),
                    DocumentChunk.procedure_code.is_not(None),
                )
                .group_by(RAGGenerationLog.message_id, DocumentChunk.procedure_code)
            )).all()

            # Per-message: top procedure(s) — top + những cái sát top (≤ _FORM_TOP_SCORE_GAP)
            top_codes_by_msg: dict[str, set[str]] = {}
            grouped: dict[str, list[tuple[str, float]]] = {}
            for msg_id, code, sc in score_rows:
                grouped.setdefault(msg_id, []).append((code, float(sc)))
            for msg_id, lst in grouped.items():
                lst.sort(key=lambda x: -x[1])
                top = lst[0][1]
                score_picks = {c for c, sc in lst if sc >= top - self._FORM_TOP_SCORE_GAP}
                # Citation tier — chỉ dùng literal mã thủ tục (không có [Nguồn N]
                # mapping vì chunk order tại audit recovery không cố định).
                content = msg_content_by_id.get(msg_id) or ""
                cited = {m for m in self._PROC_CODE_RE.findall(content)}
                if cited:
                    final = score_picks & cited
                    if not final:
                        final = cited
                else:
                    final = score_picks
                # Sort theo score, cap 2
                ordered = sorted(final, key=lambda c: -next((sc for cc, sc in lst if cc == c), 0))
                top_codes_by_msg[msg_id] = set(ordered[:2])

            # Pha 2: query form rows cho UNION các (msg_id, top_codes)
            all_top_codes = {c for s in top_codes_by_msg.values() for c in s}
            if all_top_codes:
                form_rows = (await self._db.execute(
                    select(
                        ProcedureRequirement.name,
                        ProcedureRequirement.form_name,
                        ProcedureRequirement.form_url,
                        Procedure.code,
                        Procedure.name,
                    )
                    .join(Procedure, ProcedureRequirement.procedure_id == Procedure.id)
                    .where(
                        Procedure.code.in_(all_top_codes),
                        ProcedureRequirement.form_url.is_not(None),
                    )
                )).all()

                # Index form theo procedure_code
                forms_by_code: dict[str, list[FormItem]] = {}
                for req_name, form_name, form_url, proc_code, proc_name in form_rows:
                    bucket = forms_by_code.setdefault(proc_code, [])
                    if any(f.url == form_url for f in bucket):
                        continue
                    bucket.append(FormItem(
                        name=req_name,
                        form_name=form_name,
                        url=form_url,
                        procedure_code=proc_code,
                        procedure_name=proc_name,
                    ))

                # Assign forms vào từng message theo top codes
                for msg_id, codes in top_codes_by_msg.items():
                    out: list[FormItem] = []
                    seen_url: set[str] = set()
                    for code in codes:
                        for f in forms_by_code.get(code, []):
                            if f.url in seen_url:
                                continue
                            seen_url.add(f.url)
                            out.append(f)
                    if out:
                        forms_by_msg[msg_id] = out

        msg_responses: list[MessageResponse] = []
        for m in messages:
            resp = MessageResponse.model_validate(m)
            resp.forms = forms_by_msg.get(m.id, [])
            msg_responses.append(resp)

        return SessionHistoryResponse(
            session=SessionResponse.model_validate(session),
            messages=msg_responses,
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
        # ── Guest flow: KHÔNG persist gì vào DB ──────────────────────────────
        # Tránh rác conversation_sessions + messages do truy cập ẩn danh.
        # Multi-turn được giữ bằng cách FE gửi `history` inline từ localStorage.
        if user is None:
            history = [
                {"role": t.role, "content": t.content}
                for t in (payload.history or [])
            ][-6:]  # cap 6 lượt gần nhất, như _load_history cho user đã login
            result = await _pipeline.run(
                query=payload.question,
                locality=payload.locality,
                domain=payload.domain,
                conversation_history=history,
            )
            sources = self._build_sources(result.chunks)
            forms = await self._build_forms(result.chunks, answer_text=result.answer)
            logger.info(
                f"Chat | ask | GUEST (no persist) | history={len(history)} "
                f"| fallback={result.is_fallback} | latency={result.latency_ms}ms"
            )
            return AskResponse(
                answer=result.answer,
                session_id="",     # FE guest không dùng (đã có session local)
                message_id="",
                sources=sources,
                forms=forms,
                is_fallback=result.is_fallback,
                latency_ms=result.latency_ms,
            )

        # ── Authenticated flow: persist như cũ ────────────────────────────────
        if payload.session_id:
            session = await self.get_session(payload.session_id, user)
        else:
            session = ConversationSession(
                user_id=user.id,
                is_guest=False,
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

        # Persist RAG audit trail (đã chắc chắn user is not None ở đây)
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
        forms = await self._build_forms(result.chunks, answer_text=result.answer)
        logger.info(
            f"Chat | ask | session_id={session.id} | user_id={user.id} "
            f"| fallback={result.is_fallback} | latency={result.latency_ms}ms | forms={len(forms)}"
        )

        return AskResponse(
            answer=result.answer,
            session_id=session.id,
            message_id=assistant_msg.id,
            sources=sources,
            forms=forms,
            is_fallback=result.is_fallback,
            latency_ms=result.latency_ms,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    # Chênh score tối đa giữa procedure thứ 2 và top để vẫn show form của nó.
    # Hẹp hơn so với trước (0.05 → 0.02): embedding bge-m3 trên domain TTHC
    # Việt nhiều cặp gần nhau (tạm trú/thường trú, gia hạn/cấp mới...) → cần
    # gap nhỏ để chỉ giữ procedure thực sự sát top.
    _FORM_TOP_SCORE_GAP = 0.02

    # Pattern bắt mã thủ tục dạng "1.001020", "2.000123" trong text answer.
    _PROC_CODE_RE = re.compile(r"\b\d+\.\d{4,}\b")
    _SOURCE_REF_RE = re.compile(r"\[\s*Nguồn\s+(\d+)\s*\]", re.IGNORECASE)

    def _extract_cited_codes(
        self, answer: str | None, chunks: list[RetrievedChunk]
    ) -> set[str]:
        """
        Tìm tập procedure_code mà LLM thực sự cite trong câu trả lời.

        2 nguồn:
          1. Mã thủ tục literal trong text (vd "thủ tục 1.003460").
          2. Reference dạng [Nguồn N] → map ngược về chunks[N-1].procedure_code.

        Trả về tập rỗng nếu không cite gì → caller fallback dùng score-only.
        """
        codes: set[str] = set()
        if not answer:
            return codes

        # 1. Literal code trong text
        for m in self._PROC_CODE_RE.findall(answer):
            codes.add(m)

        # 2. [Nguồn N] → chunk index
        for m in self._SOURCE_REF_RE.findall(answer):
            try:
                idx = int(m) - 1
            except ValueError:
                continue
            if 0 <= idx < len(chunks):
                code = chunks[idx].metadata.get("procedure_code")
                if code:
                    codes.add(str(code))
        return codes

    async def _build_forms(
        self,
        chunks: list[RetrievedChunk],
        answer_text: str | None = None,
    ) -> list[FormItem]:
        """
        Lấy biểu mẫu CHỈ của thủ tục thực sự được nêu trong câu trả lời.

        Filter 2 tầng:
          - Score tier: procedure có chunk score ≥ top - GAP (gap=0.02)
          - Citation tier: procedure được LLM cite (mã literal hoặc [Nguồn N])

        Giao của 2 tập = procedure_code dùng cho form. Nếu LLM không cite gì
        → fallback dùng score tier only.
        """
        from app.models.procedure import Procedure, ProcedureRequirement

        # Best score per procedure_code
        best_score: dict[str, float] = {}
        for c in chunks:
            code = c.metadata.get("procedure_code")
            if not code:
                continue
            if code not in best_score or c.score > best_score[code]:
                best_score[code] = c.score
        if not best_score:
            return []

        # Score tier
        top = max(best_score.values())
        score_codes = {
            code for code, sc in best_score.items()
            if sc >= top - self._FORM_TOP_SCORE_GAP
        }

        # Citation tier
        cited_codes = self._extract_cited_codes(answer_text, chunks)

        # Intersect — citation thắng nếu có
        if cited_codes:
            final_codes = score_codes & cited_codes
            if not final_codes:
                # Edge: LLM cite procedure score thấp → ưu tiên cited
                # (đáng tin hơn embedding score khi LLM đã đọc full context).
                final_codes = cited_codes
        else:
            final_codes = score_codes

        # Hardcap top 2 procedure để tránh spam form cards
        codes = sorted(final_codes, key=lambda c: -best_score.get(c, 0))[:2]
        if not codes:
            return []
        logger.debug(
            f"Chat | build_forms | score_top={top:.3f} | "
            f"score_codes={sorted(score_codes)} | cited={sorted(cited_codes)} | "
            f"final={codes}"
        )

        rows = (await self._db.execute(
            select(ProcedureRequirement, Procedure.name, Procedure.code)
            .join(Procedure, ProcedureRequirement.procedure_id == Procedure.id)
            .where(
                Procedure.code.in_(codes),
                ProcedureRequirement.form_url.is_not(None),
            )
        )).all()

        forms: list[FormItem] = []
        seen: set[str] = set()
        for req, proc_name, proc_code in rows:
            if not req.form_url or req.form_url in seen:
                continue
            seen.add(req.form_url)
            forms.append(FormItem(
                name=req.name,
                form_name=req.form_name,
                url=req.form_url,
                procedure_code=proc_code,
                procedure_name=proc_name,
            ))
        return forms

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
