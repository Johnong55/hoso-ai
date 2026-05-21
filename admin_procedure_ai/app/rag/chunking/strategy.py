# app/rag/chunking/strategy.py
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.models.document import ChunkType


@dataclass
class Chunk:
    content: str
    chunk_type: ChunkType
    metadata: dict[str, Any] = field(default_factory=dict)


class ProcedureChunker:
    """
    Splits a procedure document into semantic chunks.
    One chunk = one semantic unit (requirement, step, or paragraph).
    Never splits across requirements or steps.
    Uses sliding window only for long general text.
    """

    def __init__(
        self,
        chunk_size: int = settings.RAG_CHUNK_SIZE,
        chunk_overlap: int = settings.RAG_CHUNK_OVERLAP,
    ) -> None:
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap

    def chunk_procedure(self, procedure_data: dict[str, Any]) -> list[Chunk]:
        """
        procedure_data keys expected:
          id, code, name, domain, authority_level, locality,
          description, legal_basis, fee, result, processing_time,
          requirements: [{name, form_name, quantity, document_type, note, is_mandatory, order}]
          steps: [{order, title, description, responsible_party, duration}]
        """
        base_meta = {
            "procedure_id": procedure_data.get("id"),
            "procedure_code": procedure_data.get("code"),
            "domain": procedure_data.get("domain"),
            "authority_level": procedure_data.get("authority_level"),
            "locality": procedure_data.get("locality"),
        }
        chunks: list[Chunk] = []

        # General description chunk(s)
        if procedure_data.get("description"):
            for chunk in self._split_text(procedure_data["description"]):
                chunks.append(Chunk(
                    content=f"Thủ tục: {procedure_data['name']}\n{chunk}",
                    chunk_type=ChunkType.GENERAL,
                    metadata={**base_meta, "section": "Mô tả"},
                ))

        # Fee chunk
        if procedure_data.get("fee") or procedure_data.get("processing_time"):
            fee_text = f"Thủ tục: {procedure_data['name']}\n"
            if procedure_data.get("processing_time"):
                fee_text += f"Thời gian xử lý: {procedure_data['processing_time']}\n"
            if procedure_data.get("fee"):
                fee_text += f"Lệ phí: {procedure_data['fee']}\n"
            chunks.append(Chunk(
                content=fee_text.strip(),
                chunk_type=ChunkType.FEE,
                metadata={**base_meta, "section": "Lệ phí & thời gian"},
            ))

        # Result chunk
        if procedure_data.get("result"):
            chunks.append(Chunk(
                content=f"Thủ tục: {procedure_data['name']}\nKết quả giải quyết: {procedure_data['result']}",
                chunk_type=ChunkType.RESULT,
                metadata={**base_meta, "section": "Kết quả"},
            ))

        # Legal basis chunk
        if procedure_data.get("legal_basis"):
            chunks.append(Chunk(
                content=f"Thủ tục: {procedure_data['name']}\nCăn cứ pháp lý: {procedure_data['legal_basis']}",
                chunk_type=ChunkType.LEGAL_BASIS,
                metadata={**base_meta, "section": "Căn cứ pháp lý"},
            ))

        # Group requirements by case_group → 1 chunk per group
        # 4 nhóm chuẩn: "Bao gồm", "Giấy tờ phải nộp",
        #                "Giấy tờ phải xuất trình", "Lưu ý"
        # "Lưu ý" → chunk GENERAL riêng (hướng dẫn, không phải yêu cầu)
        NOTE_GROUP = "Lưu ý"
        req_groups: dict[str, list] = {}
        for req in procedure_data.get("requirements", []):
            key = req.get("case_group") or "Bao gồm"
            req_groups.setdefault(key, []).append(req)

        for case_group_key, reqs in req_groups.items():
            if case_group_key == NOTE_GROUP:
                # "Lưu ý" → GENERAL chunk (không phải danh sách giấy tờ)
                text = self._format_note_group(procedure_data["name"], reqs)
                chunks.append(Chunk(
                    content=text,
                    chunk_type=ChunkType.GENERAL,
                    metadata={
                        **base_meta,
                        "section": "Lưu ý",
                        "case_group": NOTE_GROUP,
                    },
                ))
            else:
                text = self._format_requirement_group(
                    procedure_data["name"], case_group_key, reqs
                )
                chunks.append(Chunk(
                    content=text,
                    chunk_type=ChunkType.REQUIREMENT,
                    metadata={
                        **base_meta,
                        "section": "Thành phần hồ sơ",   # luôn ngắn, tránh overflow VARCHAR(255)
                        "case_group": case_group_key[:500],
                    },
                ))

        # Form chunks — mỗi biểu mẫu đã parse thành 1 chunk riêng
        for form_data in procedure_data.get("forms", []):
            from app.crawler.parsers.form_parser import format_form_chunk
            text = format_form_chunk(form_data, procedure_data["name"])
            chunks.append(Chunk(
                content=text,
                chunk_type=ChunkType.FORM,
                metadata={
                    **base_meta,
                    "section": "Biểu mẫu",
                    "form_name": form_data.get("form_name"),
                    "form_url": form_data.get("form_url"),
                },
            ))

        # Chunk từng step — nếu description quá dài thì dùng sliding window
        for step in procedure_data.get("steps", []):
            desc = step.get("description") or ""
            sub_texts = self._split_text(desc) if len(desc) > self._chunk_size else [desc]
            for part_idx, sub_text in enumerate(sub_texts):
                step_copy = {**step, "description": sub_text}
                # Nếu chia thành nhiều phần, ghi rõ "(phần N/M)"
                if len(sub_texts) > 1:
                    step_copy["title"] = f"{step.get('title', 'Trình tự thực hiện')} (phần {part_idx + 1}/{len(sub_texts)})"
                text = self._format_step(procedure_data["name"], step_copy)
                chunks.append(Chunk(
                    content=text,
                    chunk_type=ChunkType.STEP,
                    metadata={
                        **base_meta,
                        "section": "Trình tự thực hiện",
                        "step_order": step.get("order"),
                    },
                ))

        return chunks

    # Các nhóm "loại giấy tờ" chuẩn (không phải trường hợp cụ thể)
    STANDARD_DOC_TYPES = {
        "Bao gồm",
        "Giấy tờ phải nộp",
        "Giấy tờ phải xuất trình",
    }

    def _format_requirement_group(
        self, procedure_name: str, case_group: str, reqs: list[dict]
    ) -> str:
        """
        Có 2 loại case_group:
        1. Loại giấy tờ chuẩn ("Bao gồm", "Giấy tờ phải nộp", "Giấy tờ phải xuất trình")
           → format: "Thành phần hồ sơ (loại giấy tờ):\n1. ..."
        2. Trường hợp cụ thể ("Đăng ký thường trú tại chỗ ở thuê mượn...", ...)
           → format: "Trường hợp: [mô tả]\nGiấy tờ cần nộp:\n1. ..."
           Đưa mô tả trường hợp vào đầu chunk để Cohere embed ngữ nghĩa đúng.
        """
        if case_group in self.STANDARD_DOC_TYPES:
            labels = {
                "Bao gồm":                   "Thành phần hồ sơ (biểu mẫu, giấy tờ cần nộp)",
                "Giấy tờ phải nộp":          "Giấy tờ phải nộp kèm hồ sơ",
                "Giấy tờ phải xuất trình":   "Giấy tờ phải xuất trình khi nộp hồ sơ",
            }
            lines = [
                f"Thủ tục: {procedure_name}",
                f"{labels[case_group]}:",
            ]
        else:
            # Trường hợp cụ thể — đưa mô tả đầy đủ vào chunk để embedding khớp
            # với câu hỏi người dùng mô tả tình huống của họ
            lines = [
                f"Thủ tục: {procedure_name}",
                f"Trường hợp: {case_group}",
                "Giấy tờ cần nộp:",
            ]

        for i, req in enumerate(reqs, 1):
            entry = f"{i}. {req['name']}"
            if req.get("quantity"):
                entry += f" ({req['quantity']})"
            if req.get("form_name"):
                entry += f" — Mẫu: {req['form_name']}"
            lines.append(entry)
        return "\n".join(lines)

    def _format_note_group(self, procedure_name: str, reqs: list[dict]) -> str:
        lines = [f"Thủ tục: {procedure_name}", "Lưu ý quan trọng khi thực hiện:"]
        for req in reqs:
            text = req.get("description") or req.get("name", "")
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines)

    def _format_step(self, procedure_name: str, step: dict) -> str:
        lines = [
            f"Thủ tục: {procedure_name}",
            f"Bước {step['order']}: {step['title']}",
        ]
        if step.get("description"):
            lines.append(step["description"])
        if step.get("responsible_party"):
            lines.append(f"Cơ quan thực hiện: {step['responsible_party']}")
        if step.get("duration"):
            lines.append(f"Thời gian: {step['duration']}")
        return "\n".join(lines)

    def _split_text(self, text: str) -> list[str]:
        """Sliding window split for long general text."""
        if len(text) <= self._chunk_size:
            return [text]

        chunks = []
        start = 0
        while start < len(text):
            end = start + self._chunk_size
            chunks.append(text[start:end])
            start += self._chunk_size - self._chunk_overlap
        return chunks
