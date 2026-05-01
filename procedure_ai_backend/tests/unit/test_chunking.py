# tests/unit/test_chunking.py
import pytest

from app.rag.chunking.strategy import ChunkType, ProcedureChunker


@pytest.fixture
def sample_procedure():
    return {
        "id": "proc-001",
        "code": "TTHC-001",
        "name": "Đăng ký kết hôn",
        "domain": "Hộ tịch",
        "authority_level": "commune",
        "locality": "Hà Nội",
        "description": "Thủ tục đăng ký kết hôn tại UBND cấp xã.",
        "processing_time": "3 ngày làm việc",
        "fee": "Miễn phí",
        "result": "Giấy chứng nhận kết hôn",
        "legal_basis": "Luật Hôn nhân và Gia đình 2014",
        "requirements": [
            {"name": "Tờ khai đăng ký kết hôn", "quantity": "01 bản", "is_mandatory": True, "order": 1},
            {"name": "Giấy tờ tùy thân", "quantity": "Bản sao", "is_mandatory": True, "order": 2},
        ],
        "steps": [
            {"order": 1, "title": "Nộp hồ sơ", "description": "Nộp tại bộ phận một cửa UBND xã."},
            {"order": 2, "title": "Kiểm tra hồ sơ", "description": "Cán bộ kiểm tra tính hợp lệ."},
        ],
    }


def test_chunker_produces_correct_types(sample_procedure):
    chunker = ProcedureChunker()
    chunks = chunker.chunk_procedure(sample_procedure)

    types = {c.chunk_type for c in chunks}
    assert ChunkType.GENERAL in types
    assert ChunkType.REQUIREMENT in types
    assert ChunkType.STEP in types
    assert ChunkType.FEE in types
    assert ChunkType.RESULT in types
    assert ChunkType.LEGAL_BASIS in types


def test_chunker_requirement_count(sample_procedure):
    chunker = ProcedureChunker()
    chunks = chunker.chunk_procedure(sample_procedure)
    req_chunks = [c for c in chunks if c.chunk_type == ChunkType.REQUIREMENT]
    assert len(req_chunks) == 2


def test_chunker_step_count(sample_procedure):
    chunker = ProcedureChunker()
    chunks = chunker.chunk_procedure(sample_procedure)
    step_chunks = [c for c in chunks if c.chunk_type == ChunkType.STEP]
    assert len(step_chunks) == 2


def test_chunker_metadata_propagated(sample_procedure):
    chunker = ProcedureChunker()
    chunks = chunker.chunk_procedure(sample_procedure)
    for chunk in chunks:
        assert chunk.metadata["procedure_id"] == "proc-001"
        assert chunk.metadata["domain"] == "Hộ tịch"


def test_chunker_no_requirements(sample_procedure):
    sample_procedure["requirements"] = []
    chunker = ProcedureChunker()
    chunks = chunker.chunk_procedure(sample_procedure)
    assert all(c.chunk_type != ChunkType.REQUIREMENT for c in chunks)
