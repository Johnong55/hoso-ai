# app/rag/generation/generator.py
from dataclasses import dataclass

from loguru import logger
from openai import OpenAI, APIStatusError

from app.core.config import settings
from app.rag.retrieval.retriever import RetrievedChunk

# Fallback chain — khi model chính bị 503/429/404, tự thử model khác.
# Khác nhau theo provider vì namespace model khác hẳn.
def _build_model_fallbacks() -> list[str]:
    provider = settings.LLM_PROVIDER.lower()
    if provider == "cloudflare":
        # ⚠ Các model Llama 3.x đã bị Cloudflare deprecate ngày 2026-05-30.
        # Chain hiện tại ưu tiên Llama 4 và Gemma 3 / Qwen 2.5 (còn hoạt động).
        chain = [
            settings.ACTIVE_LLM_MODEL,                              # default từ env
            "@cf/meta/llama-4-scout-17b-16e-instruct",              # Llama 4 Scout — primary
            "@cf/google/gemma-3-12b-it",                            # Gemma 3 fallback
            "@cf/qwen/qwen2.5-coder-32b-instruct",                  # Qwen 2.5
            "@cf/mistralai/mistral-small-3.1-24b-instruct",         # Mistral fallback cuối
        ]
    else:
        # OpenRouter / Gemini default chain
        chain = [
            settings.ACTIVE_LLM_MODEL,
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-2.5-flash-lite",
            "gemini-flash-latest",
        ]
    return list(dict.fromkeys(chain))


MODEL_FALLBACKS = _build_model_fallbacks()

SYSTEM_PROMPT = """Bạn là trợ lý AI thủ tục hành chính Việt Nam — chế độ INTRO.

NHIỆM VỤ:
Người dùng hỏi theo tình huống đời thường ("em bé sinh ở nước ngoài muốn có quốc
tịch VN", "tôi mất CCCD"...). Bạn cần:
  1. MAP tình huống → 1 thủ tục PHÙ HỢP NHẤT trong [NGỮ CẢNH].
  2. Trả lời NGẮN GỌN (3-5 câu, tối đa 100 từ) gồm:
     - Tên đầy đủ thủ tục + mã (vd: "Cấp thẻ tạm trú cho người nước ngoài tại
       Việt Nam tại Công an cấp tỉnh, mã 1.003460").
     - 1-2 câu giải thích vì sao thủ tục này áp dụng cho tình huống của họ.
     - 1 dòng dẫn: "Vui lòng chọn nội dung bạn muốn xem chi tiết bên dưới."
  3. KHÔNG liệt kê bước thực hiện, không liệt kê giấy tờ, không liệt kê lệ phí
     trong câu trả lời INTRO. Hệ thống sẽ hiển thị các CHIP cho user click để
     xem từng phần chi tiết riêng — đỡ tốn token, đỡ tràn màn hình.

QUY TẮC:
1. CHỈ dùng thông tin trong [NGỮ CẢNH]. KHÔNG bịa đặt, không thêm kiến thức ngoài.
2. Khi có nhiều thủ tục liên quan, chọn THỦ TỤC KHỚP NHẤT (top score) trả lời.
   Các thủ tục khác đã có chip "Xem thủ tục khác" cho user click — không nhắc
   trong text intro.
3. KIỂM TRA PHẠM VI TRƯỚC KHI TRẢ LỜI:
   - Bạn CHỈ tư vấn về thủ tục hành chính của CÔNG DÂN/TỔ CHỨC Việt Nam thực
     hiện với CƠ QUAN NHÀ NƯỚC (vd: đăng ký khai sinh, cấp CCCD, tạm trú, đăng
     ký kinh doanh, cấp hộ chiếu, đất đai, hộ tịch...).
   - Câu hỏi KHÔNG thuộc phạm vi này bao gồm: thời tiết / lời khuyên cá nhân /
     giải trí / lập trình / toán học / triết học / thủ tục NƯỚC NGOÀI / bảo hành
     thương mại / chính hệ thống AI này / các câu hỏi nhảm.
   - Nếu câu hỏi không thuộc phạm vi, BẮT BUỘC trả lời CHÍNH XÁC chuỗi sau và
     DỪNG (không thêm gì khác, không trích dẫn nguồn):
     "Tôi không tìm thấy thông tin về vấn đề này trong cơ sở dữ liệu thủ tục
     hành chính. Vui lòng liên hệ trực tiếp với cơ quan có thẩm quyền hoặc truy
     cập cổng Dịch vụ công Quốc gia tại dichvucong.gov.vn để được hỗ trợ."
   - TUYỆT ĐỐI KHÔNG "kéo dài" ý nghĩa câu hỏi để khớp với [NGỮ CẢNH]. Vd câu
     "trời nắng có nên ra ngoài không" KHÔNG được mapping sang thủ tục về khí
     tượng thủy văn, dù có context khí tượng — đó là câu hỏi đời thường, không
     phải thủ tục hành chính.
4. Trích dẫn cuối câu theo định dạng cố định (CHỈ áp dụng khi đã xác định được
   thủ tục đúng phạm vi):
   `Nguồn: Thủ tục <tên đầy đủ> (mã: <X.XXXXXX>)`
   Mã lấy literal từ metadata `[mã: ...]` trong [NGỮ CẢNH]. KHÔNG bịa mã.
5. Trả lời bằng tiếng Việt, văn phong tự nhiên, không bullet trong intro.

VÍ DỤ INTRO ĐÚNG ĐỘ DÀI:
> Để cấp thẻ tạm trú cho vợ/chồng người nước ngoài của công dân Việt Nam, thủ
> tục phù hợp là Cấp thẻ tạm trú cho người nước ngoài tại Việt Nam tại Công an
> cấp tỉnh (mã 1.003460). Cơ quan tiếp nhận là Phòng Quản lý xuất nhập cảnh
> Công an cấp tỉnh nơi vợ bạn cư trú, áp dụng cho diện cá nhân bảo lãnh
> người nước ngoài. Vui lòng chọn nội dung bạn muốn xem chi tiết bên dưới.
> Nguồn: Thủ tục Cấp thẻ tạm trú cho người nước ngoài tại Việt Nam tại Công an
> cấp tỉnh (mã: 1.003460).
"""

# Prompt dùng khi user click 1 chip → format 1 section cụ thể
SECTION_PROMPTS = {
    "steps": (
        "Bạn nhận được [DỮ LIỆU] là toàn bộ trình tự thực hiện của 1 thủ tục.\n"
        "Hãy trình bày lại RÕ RÀNG theo bullet, GIỮ NGUYÊN ý nghĩa và đầy đủ\n"
        "các Bước 1, 2, 3, ... đến hết. Không rút gọn, không bỏ sub-bullet nào.\n"
        "Mỗi bước có thể có nhiều ý nhỏ — giữ nguyên cấu trúc lồng nhau."
    ),
    "requirements": (
        "Bạn nhận được [DỮ LIỆU] là danh sách giấy tờ cần chuẩn bị cho 1 thủ tục,\n"
        "đã được nhóm theo case_group (trường hợp áp dụng). Hãy:\n"
        "  - Liệt kê đầy đủ từng giấy tờ trong từng nhóm.\n"
        "  - Ghi rõ quantity (vd 'Bản chính: 1') nếu có.\n"
        "  - Nếu có form_name → ghi: 'Mẫu: <form_name>' (FE tự render link tải).\n"
        "  - Giả định tất cả giấy tờ liệt kê ĐỀU bắt buộc, TRỪ KHI tên có chứa\n"
        "    'nếu có', 'hoặc', 'tuỳ trường hợp' — khi đó nêu rõ điều kiện áp dụng."
    ),
    "fees": (
        "Bạn nhận được [DỮ LIỆU] là danh sách phí + thời hạn theo từng phương thức nộp.\n"
        "Trình bày dưới dạng nhóm theo submission_method (Trực tiếp / Trực tuyến /\n"
        "Dịch vụ bưu chính). Trong mỗi nhóm: nêu thời hạn + mức phí + mô tả áp dụng."
    ),
    "agency": (
        "Bạn nhận được [DỮ LIỆU] về cơ quan thực hiện thủ tục. Trình bày ngắn gọn:\n"
        "  - Cơ quan thực hiện chính.\n"
        "  - Cơ quan phối hợp (nếu có).\n"
        "  - Nơi nộp hồ sơ cụ thể (cấp tỉnh / cấp huyện / Trung ương).\n"
        "  - Hướng dẫn tìm địa chỉ nếu có thông tin."
    ),
    "forms": (
        "Bạn nhận được [DỮ LIỆU] là danh sách biểu mẫu tải về. Liệt kê từng biểu\n"
        "mẫu kèm tên file. Frontend sẽ tự render nút tải. Không bịa thêm form."
    ),
    "form_guide": (
        "Bạn nhận được [DỮ LIỆU] gồm 2 phần:\n"
        "  - [NỘI DUNG FORM]: text trích từ file biểu mẫu (DOCX/PDF).\n"
        "  - [TRƯỜNG ĐÃ DETECT]: list tên trường + gợi ý điền (có thể trống nếu\n"
        "    form không có cấu trúc bảng rõ ràng).\n"
        "Mục tiêu: hướng dẫn người dân TỰ điền form này đúng cách.\n\n"
        "OUTPUT BẮT BUỘC 2 PHẦN, đúng thứ tự, dùng markdown header `##`:\n\n"
        "## Tóm tắt\n"
        "3-5 câu văn xuôi (KHÔNG bullet): mục đích biểu mẫu, ai phải khai, ai phải\n"
        "ký xác nhận, lưu ý quan trọng (bản chính/sao, công chứng, đính kèm gì).\n\n"
        "## Chi tiết từng mục\n"
        "Liệt kê các mục cần điền theo thứ tự trong form. Mỗi mục:\n"
        "**<Tên mục>**\n"
        "- Cách điền: <hướng dẫn ngắn gọn, rõ ràng>\n"
        "- Ví dụ: <ví dụ cụ thể, sát tình huống user nếu có>\n\n"
        "QUY TẮC:\n"
        "- CHỈ dựa vào [DỮ LIỆU], không bịa mục không có trong form.\n"
        "- Bỏ qua mục không rõ ý nghĩa thay vì đoán.\n"
        "- Mục lặp lại (vd cùng 1 chữ ký nhiều lần) chỉ liệt kê 1 lần.\n"
        "- Nếu form quá ngắn / không có mục cụ thể → phần Chi tiết chỉ nêu các\n"
        "  thông tin chính cần khai báo dựa trên nội dung văn bản."
    ),
    "other_procedures": (
        "Bạn nhận được [DỮ LIỆU] là vài thủ tục liên quan (score gần với TOP-1).\n"
        "Trình bày dạng list: tên + mã + 1 dòng phân biệt mỗi cái. Mời user hỏi\n"
        "tiếp nếu muốn xem chi tiết thủ tục khác."
    ),
}

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
        # Dùng ACTIVE_* để switch giữa OpenRouter / Cloudflare via LLM_PROVIDER.
        self._client = OpenAI(
            api_key=settings.ACTIVE_LLM_API_KEY,
            base_url=settings.ACTIVE_LLM_BASE_URL,
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
                model=settings.ACTIVE_LLM_MODEL,
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
                model=used_model or settings.ACTIVE_LLM_MODEL,
            )

        answer = response.choices[0].message.content or FALLBACK_RESPONSE
        usage = response.usage
        finish = response.choices[0].finish_reason

        if finish == "length":
            logger.warning(
                f"Generator | OUTPUT TRUNCATED (finish=length) | model={used_model} "
                f"| max_tokens={settings.LLM_MAX_TOKENS} → tăng LLM_MAX_TOKENS nếu cần"
            )

        # Detect LLM tự sinh fallback response (câu hỏi ngoài phạm vi) → mark
        # is_fallback=True để dashboard fallback rate phản ánh đúng. So khớp
        # 60 ký tự đầu (đủ unique, dung sai khi LLM thêm/bớt chút ít).
        _fallback_signature = FALLBACK_RESPONSE[:60].lower()
        is_fallback_answer = _fallback_signature in answer.lower()

        logger.info(
            f"Generator | model={used_model} | finish={finish} "
            f"| tokens={usage.total_tokens if usage else 0} "
            f"| completion={usage.completion_tokens if usage else 0} "
            f"| chunks_used={len(chunks)} | fallback={is_fallback_answer}"
        )

        return GenerationResult(
            answer=answer,
            is_fallback=is_fallback_answer,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            model=used_model,
        )

    def generate_section(
        self,
        section_type: str,
        procedure_name: str,
        procedure_code: str,
        raw_data: str,
        user_context: str | None = None,
    ) -> GenerationResult:
        """
        Format 1 section cụ thể của thủ tục đã xác định.

        `user_context` (câu hỏi gốc của user): nếu truyền, LLM filter các
        case_group trong raw_data chỉ giữ phần khớp tình huống của user.
        Vd thủ tục thường trú có 7 trường hợp, user chỉ hỏi về "thuê nhà"
        → trả lời chỉ trường hợp "thuê, mượn, ở nhờ".
        """
        prompt = SECTION_PROMPTS.get(section_type)
        if not prompt:
            return GenerationResult(
                answer=f"Loại nội dung '{section_type}' chưa được hỗ trợ.",
                is_fallback=True,
                prompt_tokens=0, completion_tokens=0, total_tokens=0,
                model=settings.ACTIVE_LLM_MODEL,
            )

        # Khi có user_context, thêm rule filter case_group khớp tình huống.
        # Áp dụng mạnh nhất cho requirements + forms (nhiều case_group). Các
        # section khác (steps/fees/agency) thường ít trường hợp → không cần.
        # form_guide: tận dụng user_context để chọn ví dụ ở mỗi mục.
        filter_rule = ""
        if user_context and section_type == "form_guide":
            filter_rule = (
                "\n\n[TÌNH HUỐNG CỦA USER]\n"
                f"{user_context}\n\n"
                "Ưu tiên ví dụ ở mỗi mục KHỚP tình huống user (vd user nói "
                "'thuê nhà ở HN' → ví dụ địa chỉ ghi địa chỉ HN thuê)."
            )
        elif user_context and section_type in ("requirements", "forms"):
            filter_rule = (
                "\n\n[TÌNH HUỐNG CỦA USER]\n"
                f"{user_context}\n\n"
                "QUY TẮC LỌC THEO TÌNH HUỐNG:\n"
                "- Nếu [DỮ LIỆU] có nhiều trường hợp / case_group khác nhau\n"
                "  (vd 'Trường hợp 1: ...', 'Trường hợp 2: ...'), CHỈ LIỆT KÊ\n"
                "  trường hợp khớp với tình huống của user.\n"
                "- Vd user hỏi 'thường trú khi thuê nhà' → chỉ giữ case_group\n"
                "  về 'thuê, mượn, ở nhờ', bỏ qua 'sở hữu nhà', 'tôn giáo',\n"
                "  'quân đội', v.v.\n"
                "- Nếu KHÔNG XÁC ĐỊNH được tình huống user khớp với case_group\n"
                "  nào → hiển thị 2-3 case_group khả dĩ NHẤT, kèm dòng:\n"
                "  'Trường hợp của bạn cụ thể là gì? Bạn có thể nói rõ hơn để\n"
                "  hệ thống lọc giấy tờ phù hợp.'\n"
                "- Mở đầu câu trả lời nhắc lại tình huống ngắn gọn 1 câu\n"
                "  (vd 'Với tình huống đăng ký thường trú khi thuê nhà...')\n"
                "  rồi mới liệt kê giấy tờ — để user xác nhận filter đúng."
            )

        system = (
            f"{prompt}\n\n"
            "QUY TẮC CHUNG:\n"
            "- CHỈ dùng [DỮ LIỆU], không bịa đặt.\n"
            "- Văn phong tiếng Việt rõ ràng, bullet list khi liệt kê.\n"
            "- KHÔNG thêm phần giới thiệu/kết luận dài dòng — đi thẳng nội dung.\n"
            "- Không cite Nguồn ở cuối (đã có context bên trên trong UI)."
            f"{filter_rule}"
        )
        user_msg = (
            f"[THỦ TỤC] {procedure_name} (mã: {procedure_code})\n\n"
            f"[DỮ LIỆU]\n{raw_data}"
        )
        response, used_model = self._call_with_fallback(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=settings.LLM_TEMPERATURE,
            max_tokens=settings.LLM_MAX_TOKENS,
        )
        if response is None:
            return GenerationResult(
                answer="Không tải được nội dung này. Vui lòng thử lại.",
                is_fallback=True,
                prompt_tokens=0, completion_tokens=0, total_tokens=0,
                model=used_model or settings.ACTIVE_LLM_MODEL,
            )
        answer = response.choices[0].message.content or "Nội dung trống."
        usage = response.usage
        logger.info(
            f"Generator | section={section_type} | model={used_model} "
            f"| tokens={usage.total_tokens if usage else 0}"
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
                if model != settings.ACTIVE_LLM_MODEL:
                    logger.warning(f"Generator | fallback success | dùng {model} thay vì {settings.ACTIVE_LLM_MODEL}")
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
