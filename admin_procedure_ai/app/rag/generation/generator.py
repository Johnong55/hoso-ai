# app/rag/generation/generator.py
from dataclasses import dataclass

from loguru import logger
from openai import OpenAI, APIStatusError

from app.core.config import settings
from app.rag.retrieval.retriever import RetrievedChunk

# Fallback chain — khi model chính bị 503/429/404, tự thử model khác
# Đã verify các model này tồn tại qua ListModels API (key user hiện tại)
MODEL_FALLBACKS = [
    settings.LLM_MODEL,        # Model chính từ .env
    "gemini-2.0-flash",        # Stable, hiếm overload
    "gemini-2.0-flash-lite",   # Lite version, gần như không overload
    "gemini-2.5-flash-lite",   # Lite 2.5
    "gemini-flash-latest",     # Always-latest pointer
]
# Dedupe giữ thứ tự
MODEL_FALLBACKS = list(dict.fromkeys(MODEL_FALLBACKS))

SYSTEM_PROMPT = """Bạn là trợ lý AI chuyên về thủ tục hành chính Việt Nam.

QUY TẮC BẮT BUỘC:
1. CHỈ trả lời dựa trên thông tin trong [NGỮ CẢNH] được cung cấp.
2. KHÔNG bịa đặt, suy đoán, hoặc thêm thông tin ngoài ngữ cảnh.
3. Nếu không tìm thấy thông tin trong ngữ cảnh, trả lời: "Tôi không tìm thấy thông tin về vấn đề này trong cơ sở dữ liệu. Vui lòng liên hệ cơ quan có thẩm quyền để được hỗ trợ."
4. Luôn trích dẫn nguồn (tên thủ tục) khi trả lời.
5. Trả lời bằng tiếng Việt, rõ ràng, dễ hiểu.
6. Không thêm thông tin ngoài phạm vi câu hỏi.
"""

FALLBACK_RESPONSE = (
    "Tôi không tìm thấy thông tin về vấn đề này trong cơ sở dữ liệu thủ tục hành chính. "
    "Vui lòng liên hệ trực tiếp với cơ quan có thẩm quyền hoặc truy cập "
    "cổng Dịch vụ công Quốc gia tại dichvucong.gov.vn để được hỗ trợ."
)


@dataclass
class GenerationResult:
    answer: str
    is_fallback: bool
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str


class Generator:
    """
    Generates answers from retrieved context using OpenAI.
    Enforces strict grounding — no hallucination.
    """

    def __init__(self) -> None:
        self._client = OpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
        )

    def generate(
        self,
        query: str,
        chunks: list[RetrievedChunk],
    ) -> GenerationResult:
        if not chunks:
            return GenerationResult(
                answer=FALLBACK_RESPONSE,
                is_fallback=True,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                model=settings.OPENAI_LLM_MODEL,
            )

        context = self._build_context(chunks)
        user_message = f"[NGỮ CẢNH]\n{context}\n\n[CÂU HỎI]\n{query}"

        response, used_model = self._call_with_fallback(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=settings.LLM_TEMPERATURE,
            max_tokens=settings.LLM_MAX_TOKENS,
        )

        if response is None:
            logger.warning("Generator | tất cả model fallback đều fail → trả fallback message")
            return GenerationResult(
                answer=FALLBACK_RESPONSE,
                is_fallback=True,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                model=used_model or settings.LLM_MODEL,
            )

        answer = response.choices[0].message.content or FALLBACK_RESPONSE
        usage = response.usage

        logger.info(
            f"Generator | model={used_model} "
            f"| tokens={usage.total_tokens if usage else 0} "
            f"| chunks_used={len(chunks)}"
        )

        return GenerationResult(
            answer=answer,
            is_fallback=False,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            model=used_model,
        )

    def rewrite_query(self, query: str, history: list[dict] | None = None) -> str:
        """
        Rewrite a user query for better retrieval, optionally using conversation history.
        temperature=0.0 for deterministic output.
        """
        history_text = ""
        if history:
            for msg in history[-4:]:
                role = "Người dùng" if msg["role"] == "user" else "Trợ lý"
                history_text += f"{role}: {msg['content']}\n"

        prompt = (
            "Viết lại câu hỏi sau thành một câu tìm kiếm rõ ràng về thủ tục hành chính Việt Nam. "
            "Chỉ trả về câu đã viết lại, không giải thích.\n\n"
        )
        if history_text:
            prompt += f"Lịch sử hội thoại:\n{history_text}\n\n"
        prompt += f"Câu hỏi: {query}"

        response, _ = self._call_with_fallback(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=150,
        )
        if response is None:
            # Tất cả model down → dùng query gốc, không rewrite
            return query
        rewritten = response.choices[0].message.content
        return (rewritten or query).strip()

    def _call_with_fallback(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ):
        """
        Gọi LLM với fallback chain — khi 1 model bị 503/429/500, tự thử model kế tiếp.
        Trả về (response, model_name_đã_dùng). Nếu tất cả fail → (None, None).
        """
        last_error = None
        for model in MODEL_FALLBACKS:
            try:
                response = self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                if model != settings.LLM_MODEL:
                    logger.warning(f"Generator | fallback success | dùng {model} thay vì {settings.LLM_MODEL}")
                return response, model
            except APIStatusError as exc:
                # Retry với: 503 (overload), 429 (rate limit), 500 (internal),
                # 404 (model bị deprecate/rename → thử model khác)
                if exc.status_code in (404, 429, 500, 503):
                    logger.warning(f"Generator | model {model} → {exc.status_code} | thử model kế tiếp")
                    last_error = exc
                    continue
                # 400/401/403 → lỗi setting hoặc auth, retry vô nghĩa
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(f"Generator | model {model} → {type(exc).__name__}: {exc} | thử model kế tiếp")
                continue

        logger.error(f"Generator | ALL fallback models failed | last_error={last_error}")
        return None, None

    def _build_context(self, chunks: list[RetrievedChunk]) -> str:
        parts = []
        for i, chunk in enumerate(chunks, 1):
            procedure_name = chunk.metadata.get("procedure_name", "")
            chunk_type = chunk.metadata.get("chunk_type", "")
            parts.append(f"[Nguồn {i}] {procedure_name} ({chunk_type})\n{chunk.content}")
        return "\n\n---\n\n".join(parts)
