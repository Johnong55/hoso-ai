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
    ProcedureFocus,
    RelatedProcedure,
    SECTION_TYPES,
    SectionRequest,
    SectionResponse,
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

        # Re-derive procedure_focus cho mỗi assistant intro message — để chip
        # vẫn hiện sau navigate / reload. Chỉ cho message KHÔNG phải section
        # (section messages không có chip riêng).
        focus_by_msg = await self._rederive_focus_for_history(
            messages, top_codes_by_msg if assistant_ids else {}
        )

        msg_responses: list[MessageResponse] = []
        for m in messages:
            resp = MessageResponse.model_validate(m)
            resp.forms = forms_by_msg.get(m.id, [])
            resp.procedure_focus = focus_by_msg.get(m.id)
            msg_responses.append(resp)

        return SessionHistoryResponse(
            session=SessionResponse.model_validate(session),
            messages=msg_responses,
        )

    async def _rederive_focus_for_history(
        self,
        messages: list[Message],
        top_codes_by_msg: dict[str, set[str]],
    ) -> dict[str, ProcedureFocus]:
        """
        Cho mỗi assistant intro message, build lại ProcedureFocus từ:
        - TOP procedure_code = top score trong audit log (top_codes_by_msg).
        - chips = adaptive theo data thực có trong DB.
        - related = TOP-2, TOP-3 trong audit.

        Heuristic phân biệt intro vs section: intro messages có chip
        re-derive (có procedure_code TOP từ audit). Section messages
        thường không có audit log (do request_section không gọi pipeline)
        → sẽ không có procedure_code → bỏ qua.
        """
        from app.models.procedure import (
            Procedure, ProcedureFee, ProcedureRequirement, ProcedureStep,
        )

        out: dict[str, ProcedureFocus] = {}
        if not top_codes_by_msg:
            return out

        # Tất cả unique codes cần lookup metadata
        all_codes: set[str] = set()
        for codes in top_codes_by_msg.values():
            all_codes.update(codes)
        if not all_codes:
            return out

        # Bulk lookup Procedure
        proc_rows = (await self._db.execute(
            select(Procedure).where(Procedure.code.in_(all_codes))
        )).scalars().all()
        proc_by_code = {p.code: p for p in proc_rows}

        # Bulk count: steps / requirements / fees / form-having-requirements
        proc_ids = [p.id for p in proc_rows]
        if not proc_ids:
            return out

        has_steps = {pid for (pid,) in (await self._db.execute(
            select(ProcedureStep.procedure_id).where(ProcedureStep.procedure_id.in_(proc_ids))
        )).all()}
        has_reqs = {pid for (pid,) in (await self._db.execute(
            select(ProcedureRequirement.procedure_id).where(
                ProcedureRequirement.procedure_id.in_(proc_ids)
            )
        )).all()}
        has_fees = {pid for (pid,) in (await self._db.execute(
            select(ProcedureFee.procedure_id).where(ProcedureFee.procedure_id.in_(proc_ids))
        )).all()}
        has_forms = {pid for (pid,) in (await self._db.execute(
            select(ProcedureRequirement.procedure_id).where(
                ProcedureRequirement.procedure_id.in_(proc_ids),
                ProcedureRequirement.form_url.is_not(None),
            )
        )).all()}

        # Build focus per message
        for msg_id, codes in top_codes_by_msg.items():
            if not codes:
                continue
            # codes là set — TOP-1 lấy được nhờ message content có cite literal mã
            # hoặc score order đã sorted. Lấy procedure_code đầu tiên có trong DB.
            top_code = next((c for c in codes if c in proc_by_code), None)
            if not top_code:
                continue
            proc = proc_by_code[top_code]
            chips: list[str] = []
            if proc.id in has_steps:
                chips.append("steps")
            if proc.id in has_reqs:
                chips.append("requirements")
            if proc.id in has_fees or proc.fee or proc.processing_time:
                chips.append("fees")
            if proc.implementing_agency or proc.authority:
                chips.append("agency")
            if proc.id in has_forms:
                chips.append("forms")

            related = [
                RelatedProcedure(code=c, name=proc_by_code[c].name)
                for c in codes
                if c != top_code and c in proc_by_code
            ][: self._MAX_RELATED]
            if related:
                chips.append("other_procedures")

            out[msg_id] = ProcedureFocus(
                code=proc.code,
                name=proc.name,
                available_chips=chips,
                related=related,
            )
        return out

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
            focus = await self._build_procedure_focus(result.chunks, result.answer)
            logger.info(
                f"Chat | ask | GUEST (no persist) | history={len(history)} "
                f"| fallback={result.is_fallback} | latency={result.latency_ms}ms "
                f"| focus={focus.code if focus else None}"
            )
            return AskResponse(
                answer=result.answer,
                session_id="",     # FE guest không dùng (đã có session local)
                message_id="",
                sources=sources,
                forms=forms,
                is_fallback=result.is_fallback,
                latency_ms=result.latency_ms,
                procedure_focus=focus,
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
        focus = await self._build_procedure_focus(result.chunks, result.answer)
        logger.info(
            f"Chat | ask | session_id={session.id} | user_id={user.id} "
            f"| fallback={result.is_fallback} | latency={result.latency_ms}ms "
            f"| forms={len(forms)} | focus={focus.code if focus else None}"
        )

        return AskResponse(
            answer=result.answer,
            session_id=session.id,
            message_id=assistant_msg.id,
            sources=sources,
            forms=forms,
            is_fallback=result.is_fallback,
            latency_ms=result.latency_ms,
            procedure_focus=focus,
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

    # ── Procedure focus + chip section ────────────────────────────────────────

    # Gap nhỏ để phân biệt "thủ tục liên quan" với "không liên quan" cho chip
    # "Xem thủ tục khác" — tránh show chip với procedure score quá thấp.
    _RELATED_SCORE_GAP = 0.08
    _MAX_RELATED = 3

    async def _build_procedure_focus(
        self,
        chunks: list[RetrievedChunk],
        answer_text: str | None,
    ) -> ProcedureFocus | None:
        """
        Xác định TOP-1 procedure (ưu tiên LLM đã cite) → fetch metadata +
        adaptive chip list dựa trên dữ liệu thực sự có. Trả None nếu không
        có procedure nào đủ tin cậy → FE không render chip.
        """
        from app.models.procedure import (
            Procedure, ProcedureFee, ProcedureRequirement, ProcedureStep,
        )

        if not chunks:
            return None

        # Best score per procedure
        best_score: dict[str, float] = {}
        name_by_code: dict[str, str] = {}
        for c in chunks:
            code = c.metadata.get("procedure_code")
            if not code:
                continue
            if code not in best_score or c.score > best_score[code]:
                best_score[code] = c.score
            if code not in name_by_code:
                name_by_code[code] = c.metadata.get("procedure_name") or ""
        if not best_score:
            return None

        # Ưu tiên procedure được LLM cite trong answer; nếu không cite → score top
        cited = self._extract_cited_codes(answer_text, chunks)
        if cited:
            top_code = max(
                cited,
                key=lambda c: best_score.get(c, 0.0),
            )
        else:
            top_code = max(best_score, key=lambda c: best_score[c])

        # Lookup procedure đầy đủ
        proc = (await self._db.execute(
            select(Procedure).where(Procedure.code == top_code)
        )).scalar_one_or_none()
        if not proc:
            return None

        # Adaptive chip — chỉ show chip nếu DB có data section đó
        chips: list[str] = []
        # steps
        has_steps = (await self._db.execute(
            select(func.count()).select_from(ProcedureStep).where(
                ProcedureStep.procedure_id == proc.id
            )
        )).scalar() or 0
        if has_steps:
            chips.append("steps")
        # requirements
        has_reqs = (await self._db.execute(
            select(func.count()).select_from(ProcedureRequirement).where(
                ProcedureRequirement.procedure_id == proc.id
            )
        )).scalar() or 0
        if has_reqs:
            chips.append("requirements")
        # fees (chip dùng cho cả fee + processing_time)
        has_fees = (await self._db.execute(
            select(func.count()).select_from(ProcedureFee).where(
                ProcedureFee.procedure_id == proc.id
            )
        )).scalar() or 0
        if has_fees or proc.fee or proc.processing_time:
            chips.append("fees")
        # agency luôn có (implementing_agency hoặc authority)
        if proc.implementing_agency or proc.authority:
            chips.append("agency")
        # forms: chỉ có khi requirement nào có form_url
        has_forms = (await self._db.execute(
            select(func.count()).select_from(ProcedureRequirement).where(
                ProcedureRequirement.procedure_id == proc.id,
                ProcedureRequirement.form_url.is_not(None),
            )
        )).scalar() or 0
        if has_forms:
            chips.append("forms")

        # Related procedures cho chip "Xem thủ tục khác"
        related_codes = sorted(
            [c for c in best_score if c != top_code],
            key=lambda c: -best_score[c],
        )
        top_score = best_score[top_code]
        related_codes = [
            c for c in related_codes
            if best_score[c] >= top_score - self._RELATED_SCORE_GAP
        ][: self._MAX_RELATED]

        related: list[RelatedProcedure] = []
        if related_codes:
            related_rows = (await self._db.execute(
                select(Procedure.code, Procedure.name).where(
                    Procedure.code.in_(related_codes)
                )
            )).all()
            related = [
                RelatedProcedure(code=code, name=name)
                for code, name in related_rows
            ]
            if related:
                chips.append("other_procedures")

        return ProcedureFocus(
            code=proc.code,
            name=proc.name,
            available_chips=chips,
            related=related,
        )

    # ── Section: trả lời 1 chip ───────────────────────────────────────────────

    async def request_section(
        self,
        payload: SectionRequest,
        user: User | None,
    ) -> SectionResponse:
        """
        User click 1 chip → format section đó cho procedure đã chọn → append
        message AI mới vào session hiện tại. Section đi qua LLM với prompt
        focused (không phải full RAG pipeline) → nhanh + consistent.
        """
        import time
        from app.models.procedure import (
            Procedure, ProcedureFee, ProcedureRequirement, ProcedureStep,
        )

        start = time.monotonic()
        section_type = payload.section_type.strip()
        if section_type not in SECTION_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"section_type không hợp lệ. Cho phép: {list(SECTION_TYPES)}",
            )

        # Lookup procedure
        proc = (await self._db.execute(
            select(Procedure).where(Procedure.code == payload.procedure_code.strip())
        )).scalar_one_or_none()
        if not proc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Không tìm thấy thủ tục mã {payload.procedure_code}.",
            )

        # Build raw_data theo section type
        raw_data = await self._build_section_raw_data(
            section_type, proc, payload.procedure_code
        )
        if not raw_data:
            answer = f"Thủ tục này chưa có dữ liệu mục '{SECTION_TYPES[section_type]}'."
            section_forms: list[FormItem] = []
        else:
            # Gọi LLM format
            gen = _pipeline._generator.generate_section(
                section_type=section_type,
                procedure_name=proc.name,
                procedure_code=proc.code,
                raw_data=raw_data,
            )
            answer = gen.answer
            section_forms = []
            # forms chip: đính kèm FormItem objects để FE render nút Tải về
            if section_type == "forms":
                req_rows = (await self._db.execute(
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
                for name, fname, url in req_rows:
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    section_forms.append(FormItem(
                        name=name, form_name=fname, url=url,
                        procedure_code=proc.code,
                        procedure_name=proc.name,
                    ))

        # Persist message
        session_id = payload.session_id or ""
        message_id = ""
        if user is not None and session_id:
            session = await self.get_session(session_id, user)
            assistant_msg = Message(
                session_id=session.id,
                role=MessageRole.ASSISTANT,
                content=answer,
            )
            self._db.add(assistant_msg)
            await self._db.flush()
            message_id = assistant_msg.id

        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            f"Chat | section | code={proc.code} | type={section_type} "
            f"| latency={elapsed_ms}ms | forms={len(section_forms)}"
        )
        return SectionResponse(
            answer=answer,
            session_id=session_id,
            message_id=message_id,
            forms=section_forms,
            procedure_code=proc.code,
            section_type=section_type,
            latency_ms=elapsed_ms,
        )

    async def _build_section_raw_data(
        self,
        section_type: str,
        proc,
        procedure_code: str,
    ) -> str:
        """Fetch + format raw data cho 1 section type. Trả empty str nếu rỗng."""
        from app.models.procedure import (
            Procedure, ProcedureFee, ProcedureRequirement, ProcedureStep,
        )

        if section_type == "steps":
            row = (await self._db.execute(
                select(ProcedureStep.description).where(
                    ProcedureStep.procedure_id == proc.id
                ).order_by(ProcedureStep.step_order)
            )).first()
            return (row[0] or "").strip() if row else ""

        if section_type == "requirements":
            rows = (await self._db.execute(
                select(ProcedureRequirement).where(
                    ProcedureRequirement.procedure_id == proc.id
                ).order_by(ProcedureRequirement.order)
            )).scalars().all()
            if not rows:
                return ""
            # Group by case_group
            from collections import defaultdict
            grouped: dict[str, list] = defaultdict(list)
            for r in rows:
                grouped[r.case_group or "Bao gồm"].append(r)
            parts = []
            for case, reqs in grouped.items():
                parts.append(f"[Trường hợp / Loại giấy tờ: {case}]")
                for r in reqs:
                    line = f"- {r.name}"
                    if r.quantity:
                        line += f" ({r.quantity})"
                    if r.form_name:
                        line += f" | Mẫu: {r.form_name}"
                    if r.is_mandatory is False:
                        line += " (không bắt buộc)"
                    parts.append(line)
                parts.append("")
            return "\n".join(parts).strip()

        if section_type == "fees":
            rows = (await self._db.execute(
                select(ProcedureFee).where(
                    ProcedureFee.procedure_id == proc.id
                ).order_by(ProcedureFee.order)
            )).scalars().all()
            if not rows:
                # fallback denorm fields
                lines = []
                if proc.processing_time:
                    lines.append(f"Thời hạn giải quyết: {proc.processing_time}")
                if proc.fee:
                    lines.append(f"Lệ phí: {proc.fee}")
                return "\n".join(lines)
            parts = []
            for r in rows:
                line = f"- Phương thức: {r.submission_method}"
                if r.processing_time:
                    line += f" | Thời hạn: {r.processing_time}"
                if r.amount_text:
                    line += f" | Phí: {r.amount_text}"
                else:
                    line += " | Phí: (không quy định)"
                parts.append(line)
                if r.description:
                    parts.append(f"  Áp dụng: {r.description}")
            return "\n".join(parts)

        if section_type == "agency":
            lines = []
            if proc.implementing_agency:
                lines.append(f"Cơ quan thực hiện: {proc.implementing_agency}")
            if proc.authority and proc.authority != proc.implementing_agency:
                lines.append(f"Cơ quan có thẩm quyền: {proc.authority}")
            if proc.coordinating_agency:
                lines.append(f"Cơ quan phối hợp: {proc.coordinating_agency}")
            if proc.authority_level:
                lines.append(f"Cấp: {proc.authority_level}")
            return "\n".join(lines)

        if section_type == "forms":
            rows = (await self._db.execute(
                select(ProcedureRequirement).where(
                    ProcedureRequirement.procedure_id == proc.id,
                    ProcedureRequirement.form_url.is_not(None),
                )
            )).scalars().all()
            if not rows:
                return ""
            seen: set[str] = set()
            parts = []
            for r in rows:
                if r.form_url in seen:
                    continue
                seen.add(r.form_url)
                line = f"- {r.name}"
                if r.form_name:
                    line += f" — file: {r.form_name}"
                parts.append(line)
            return "\n".join(parts)

        if section_type == "other_procedures":
            # Không cần LLM cho cái này — trả text trống để bypass
            return ""

        return ""
