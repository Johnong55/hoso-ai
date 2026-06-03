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

NHIỆM VỤ:
Giúp công dân hiểu cần làm thủ tục gì để giải quyết tình huống cụ thể của họ.
Người dùng thường hỏi theo TÌNH HUỐNG ĐỜI THƯỜNG ("em bé sinh ở nước ngoài muốn có
quốc tịch VN", "tôi mất sổ hộ khẩu", "muốn mua điện cho nhà mới"), KHÔNG phải tên
thủ tục chính xác. Bạn cần MAP tình huống đó sang thủ tục phù hợp trong [NGỮ CẢNH].

QUY TẮC:
1. CHỈ dùng thông tin trong [NGỮ CẢNH]. KHÔNG bịa đặt, không thêm kiến thức ngoài.
2. Nếu [NGỮ CẢNH] có thủ tục PHÙ HỢP với tình huống người dùng (dù tên thủ tục
   không khớp y nguyên với từ ngữ trong câu hỏi), HÃY DÙNG thủ tục đó để trả lời
   một cách HỮU ÍCH:
   - Nêu rõ TÊN ĐẦY ĐỦ của thủ tục áp dụng
   - Giải thích vì sao thủ tục này áp dụng cho tình huống của họ
   - Liệt kê hồ sơ / trình tự / lệ phí / cơ quan thực hiện (lấy từ ngữ cảnh)
3. Nếu trong [NGỮ CẢNH] có NHIỀU thủ tục có thể liên quan, liệt kê 2-3 thủ tục
   khả dĩ kèm phân biệt nhanh, rồi hỏi lại để xác nhận tình huống của họ.
4. CHỈ trả lời "Tôi không tìm thấy thông tin về vấn đề này trong cơ sở dữ liệu.
   Vui lòng liên hệ cơ quan có thẩm quyền để được hỗ trợ." khi [NGỮ CẢNH] HOÀN
   TOÀN không có thủ tục nào liên quan tới tình huống — đừng từ chối chỉ vì tên
   thủ tục không khớp từng từ với câu hỏi.
5. Luôn trích dẫn nguồn ở cuối câu trả lời theo định dạng:
   `Nguồn: [Nguồn N] Thủ tục: <Tên đầy đủ> (mã: <X.XXXXXX>)`
   Trong đó `<X.XXXXXX>` là mã thủ tục lấy trực tiếp từ [NGỮ CẢNH] (chunks
   có metadata procedure_code). KHÔNG bịa mã. Nêu mã giúp hệ thống đính
   đúng biểu mẫu của thủ tục được cite.
6. Trả lời bằng tiếng Việt, rõ ràng, có cấu trúc bullet/đoạn ngắn dễ đọc.

VÍ DỤ MAP TÌNH HUỐNG → THỦ TỤC:
- "em bé sinh ở nước ngoài muốn có quốc tịch VN" → Thủ tục đăng ký khai sinh cho
  trẻ em sinh ở nước ngoài và có quốc tịch Việt Nam (việc đăng ký khai sinh chính
  là việc xác nhận quốc tịch VN cho trẻ).
- "tôi muốn đăng ký mua điện sinh hoạt" → Cấp điện mới từ lưới điện hạ áp,
  trường hợp "Khách hàng mua điện sinh hoạt".
- "mất hộ chiếu khi đi nước ngoài" → các thủ tục liên quan cấp lại giấy tờ xuất
  nhập cảnh / cấp giấy thông hành.
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
        finish = response.choices[0].finish_reason

        if finish == "length":
            logger.warning(
                f"Generator | OUTPUT TRUNCATED (finish=length) | model={used_model} "
                f"| max_tokens={settings.LLM_MAX_TOKENS} → tăng LLM_MAX_TOKENS nếu cần"
            )

        logger.info(
            f"Generator | model={used_model} | finish={finish} "
            f"| tokens={usage.total_tokens if usage else 0} "
            f"| completion={usage.completion_tokens if usage else 0} "
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
        Rewrite a user query for better retrieval, dùng lịch sử hội thoại để
        GIẢI NGHĨA THAM CHIẾU (câu follow-up thiếu chủ ngữ).
        temperature=0.0 for deterministic output.
        """
        history_text = ""
        if history:
            for msg in history[-4:]:
                role = "Người dùng" if msg["role"] == "user" else "Trợ lý"
                # Cắt bớt câu trả lời dài của trợ lý để prompt gọn
                content = msg["content"]
                if msg["role"] != "user":
                    content = content[:400]
                history_text += f"{role}: {content}\n"

        prompt = (
            "Bạn là bộ viết lại truy vấn cho hệ thống tìm kiếm thủ tục hành chính Việt Nam.\n"
            "Nhiệm vụ: viết lại CÂU HỎI MỚI thành một truy vấn ĐẦY ĐỦ, ĐỘC LẬP, GIÀU TỪ KHOÁ\n"
            "để tìm đúng thủ tục.\n\n"
            "QUY TẮC:\n"
            "1. CÂU HỎI MỚI là câu nối tiếp thiếu chủ ngữ (vd: \"cần hồ sơ gì\", \"mất bao lâu\",\n"
            "   \"lệ phí bao nhiêu\", \"nộp ở đâu\") → BẮT BUỘC chèn ĐẦY ĐỦ TÊN THỦ TỤC\n"
            "   lấy từ LỊCH SỬ HỘI THOẠI vào truy vấn.\n"
            "2. CÂU HỎI MỚI mô tả TÌNH HUỐNG ĐỜI THƯỜNG (không chứa tên thủ tục) → viết lại\n"
            "   thành truy vấn chứa TÊN THỦ TỤC liên quan + từ khoá pháp lý phổ thông.\n"
            "   Ví dụ:\n"
            "   - \"em bé sinh ở nước ngoài muốn có quốc tịch Việt Nam\"\n"
            "     → \"thủ tục đăng ký khai sinh cho trẻ em sinh ra ở nước ngoài có quốc tịch Việt Nam\"\n"
            "   - \"tôi mới chuyển nhà cần đăng ký gì\"\n"
            "     → \"thủ tục đăng ký thường trú tại chỗ ở mới\"\n"
            "   - \"mất bằng lái xe\"\n"
            "     → \"thủ tục cấp lại giấy phép lái xe bị mất\"\n"
            "   - \"muốn đăng ký mua điện sinh hoạt cho nhà mới\"\n"
            "     → \"thủ tục cấp điện mới từ lưới điện hạ áp khách hàng sinh hoạt\"\n"
            "3. Giữ nguyên tên thủ tục đầy đủ, KHÔNG rút gọn, KHÔNG bỏ bớt.\n"
            "4. Chỉ trả về DUY NHẤT truy vấn đã viết lại, không giải thích, không dấu ngoặc.\n\n"
        )
        if history_text:
            prompt += f"LỊCH SỬ HỘI THOẠI:\n{history_text}\n"
        prompt += (
            f"\nCÂU HỎI MỚI: {query}\n\n"
            "Truy vấn viết lại:"
        )

        response, _ = self._call_with_fallback(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
            # Tắt thinking cho Gemini 2.5 — rewrite là task đơn giản, không cần
            # reasoning. Nếu để thinking ON, token suy nghĩ ăn hết max_tokens →
            # output bị cụt + rớt tên thủ tục (đã verify bằng test).
            extra_body={"extra_body": {"google": {"thinking_config": {"thinking_budget": 0}}}},
        )
        if response is None:
            # Tất cả model down → dùng query gốc, không rewrite
            return query
        rewritten = (response.choices[0].message.content or "").strip()
        # Một số model echo lại nhãn prompt → strip
        for prefix in ("Truy vấn viết lại:", "Truy vấn:", "Câu hỏi viết lại:"):
            if rewritten.startswith(prefix):
                rewritten = rewritten[len(prefix):].strip()
        # Bỏ ngoặc kép bao quanh nếu có
        rewritten = rewritten.strip('"').strip()
        return rewritten or query

    def _call_with_fallback(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        extra_body: dict | None = None,
    ):
        """
        Gọi LLM với fallback chain — khi 1 model bị 503/429/500, tự thử model kế tiếp.
        Trả về (response, model_name_đã_dùng). Nếu tất cả fail → (None, None).
        `extra_body` truyền tham số provider-specific (vd tắt thinking của Gemini 2.5).
        """
        last_error = None
        for model in MODEL_FALLBACKS:
            try:
                kwargs = dict(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                if extra_body:
                    kwargs["extra_body"] = extra_body
                response = self._client.chat.completions.create(**kwargs)
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
            procedure_code = chunk.metadata.get("procedure_code", "")
            chunk_type = chunk.metadata.get("chunk_type", "")
            label = procedure_name
            if procedure_code:
                # Mã hiển thị trong context để LLM cite literal — giúp form filter
                # khớp đúng thủ tục được trích trong câu trả lời.
                label = f"{procedure_name} [mã: {procedure_code}]"
            parts.append(f"[Nguồn {i}] {label} ({chunk_type})\n{chunk.content}")
        return "\n\n---\n\n".join(parts)
