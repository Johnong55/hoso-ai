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

        # One chunk per requirement — never split
        for req in procedure_data.get("requirements", []):
            text = self._format_requirement(procedure_data["name"], req)
            chunks.append(Chunk(
                content=text,
                chunk_type=ChunkType.REQUIREMENT,
                metadata={**base_meta, "section": "Thành phần hồ sơ", "step_order": req.get("order")},
            ))

        # One chunk per step — never split
        for step in procedure_data.get("steps", []):
            text = self._format_step(procedure_data["name"], step)
            chunks.append(Chunk(
                content=text,
                chunk_type=ChunkType.STEP,
                metadata={**base_meta, "section": "Trình tự thực hiện", "step_order": step.get("order")},
            ))

        return chunks

    def _format_requirement(self, procedure_name: str, req: dict) -> str:
        lines = [f"Thủ tục: {procedure_name}", f"Thành phần hồ sơ: {req['name']}"]
        if req.get("quantity"):
            lines.append(f"Số lượng: {req['quantity']}")
        if req.get("document_type"):
            lines.append(f"Loại giấy tờ: {req['document_type']}")
        if req.get("form_name"):
            lines.append(f"Mẫu đơn: {req['form_name']}")
        lines.append(f"Bắt buộc: {'Có' if req.get('is_mandatory', True) else 'Không'}")
        if req.get("note"):
            lines.append(f"Ghi chú: {req['note']}")
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
