"""
Parser cho file Word chi tiết thủ tục hành chính tải từ:
  https://thutuc.dichvucong.gov.vn/jsp/tthc/export/export_word_detail_tthc.jsp

File có dạng Office Open XML (.docx) dù extension là .doc.
Cấu trúc XML của file này không hoàn toàn chuẩn (thiếu <w:tblGrid> nên python-docx
không parse được tables) → ta đọc trực tiếp word/document.xml và lấy text các <w:p>.

Output dict shape:
{
    "code": "1.015028",
    "name": "Cấp Giấy chứng nhận...",
    "decision_number": "936/QĐ-BTC",
    "authority_level_text": "Cấp Bộ",
    "procedure_type": "TTHC được luật giao quy định chi tiết",
    "domain": "Chứng khoán",
    "object": "Doanh nghiệp",
    "implementing_agency": "Ủy ban Chứng khoán Nhà nước - Bộ tài chính",
    "competent_agency": "...",
    "address": "...",
    "delegated_agency": "...",
    "coordinating_agency": "...",
    "result": "...",
    "conditions": "...",
    "keywords": "...",
    "description": "...",
    "steps_text": "Bước 1: ...\nBước 2: ...",       # SINGLE BLOB
    "fees": [
        {"submission_method": "Trực tiếp",
         "processing_time": "30 Ngày",
         "amount_text": "5 triệu Đồng",
         "description": "Đối với cấp Giấy chứng nhận..."},
        ...
    ],
    "requirements": [
        {"name": "...",
         "case_group": "Đối với chấp thuận cho phép...",
         "form_name": "Phlcs07.docx",
         "quantity": "Bản chính: 1Bản sao: 1"},
        ...
    ],
    "legal_basis_items": [
        {"code": "135/2015/NĐ-CP",
         "title": "Nghị định 135/2015/NĐ-CP",
         "issue_date": "31-12-2015",
         "issuer": "Chính phủ"},
        ...
    ],
    "legal_basis": "135/2015/NĐ-CP: Nghị định...\n...",  # formatted text
    "fee_summary": "5 - 10 triệu Đồng",                   # for Procedure.fee denorm
    "processing_time": "30 Ngày",                         # for Procedure.processing_time
}
"""
from __future__ import annotations

import html
import re
import zipfile
from io import BytesIO
from typing import Any

from loguru import logger


# ── Helpers ────────────────────────────────────────────────────────────────────

_P_RE = re.compile(r"<w:p[\s>].*?</w:p>", re.DOTALL)
_T_RE = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.DOTALL)


def _extract_paragraphs(docx_bytes: bytes) -> list[str]:
    """Read word/document.xml, return list of paragraph text (non-empty)."""
    with zipfile.ZipFile(BytesIO(docx_bytes)) as z:
        xml = z.read("word/document.xml").decode("utf-8")

    out: list[str] = []
    for pmatch in _P_RE.findall(xml):
        texts = _T_RE.findall(pmatch)
        joined = "".join(texts).strip()
        if joined:
            # Decode XML entities: &lt; → <, &gt; → >, &amp; → &, &#xNN; → unicode
            joined = html.unescape(joined)
            out.append(joined)
    return out


def _kv_after(paragraphs: list[str], label: str) -> str | None:
    """Find paragraph starting with `<label>:` and return text after the colon."""
    prefix = label + ":"
    for p in paragraphs:
        if p.startswith(prefix):
            return p[len(prefix):].strip() or None
    return None


# Field labels (left side of header section, before "Trình tự thực hiện:")
_HEADER_LABELS = {
    "Mã thủ tục":      "code",
    "Số quyết định":   "decision_number",
    "Tên thủ tục":     "name",
    "Cấp thực hiện":   "authority_level_text",
    "Loại thủ tục":    "procedure_type",
    "Lĩnh vực":        "domain",
}

# Labels in the trailing meta block (after requirements, before legal basis)
_META_LABELS = {
    "Đối tượng thực hiện":  "object",
    "Cơ quan thực hiện":    "implementing_agency",
    "Cơ quan có thẩm quyền": "competent_agency",
    "Địa chỉ tiếp nhận HS":  "address",
    "Cơ quan được ủy quyền": "delegated_agency",
    "Cơ quan phối hợp":      "coordinating_agency",
    "Kết quả thực hiện":     "result",
    "Yêu cầu, điều kiện thực hiện": "conditions",
    "Từ khóa":              "keywords",
    "Mô tả":                "description",
}

# Section header strings used to delimit blocks
SECT_STEPS    = "Trình tự thực hiện:"
SECT_METHOD   = "Cách thức thực hiện:"
SECT_REQS     = "Thành phần hồ sơ:"
SECT_LEGAL    = "Căn cứ pháp lý:"

# Sentinels in the fees table header
_FEE_HEADER_CELLS = {"Hình thức nộp", "Thời hạn giải quyết", "Phí, lệ phí", "Mô tả"}

# Requirements table column headers (we skip these when scanning)
_REQ_HEADER_CELLS = {"Tên giấy tờ", "Mẫu đơn, tờ khai", "Số lượng"}

# Legal basis table column headers
_LEGAL_HEADER_CELLS = {"Số ký hiệu", "Trích yếu", "Ngày ban hành", "Cơ quan ban hành"}


_NULL_SENTINELS = {"Không có thông tin", ""}


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    if v in _NULL_SENTINELS:
        return None
    return v


# ── Section parsers ───────────────────────────────────────────────────────────

def _parse_fees(block: list[str]) -> list[dict[str, Any]]:
    """
    The 'Cách thức thực hiện' table is flattened to a paragraph sequence:
        [header cells × 4]
        <method>           e.g. "Trực tiếp"
        <processing_time>  e.g. "30 Ngày"
        <fee_line>...      e.g. "Phí : 5 triệu Đồng (...desc...)"
        <fee_line>
        ...
        <method description / address>  ← often "Trực tiếp tại Bộ phận Một cửa"
        <method>           next submission method row
        ...

    Algorithm: skip header cells, then iterate. Each time we see a new method
    label ("Trực tiếp"/"Trực tuyến"/"Dịch vụ bưu chính"), open a new group.
    Lines starting with "Phí" are fee rows. Other lines are skipped (descriptions).
    """
    out: list[dict[str, Any]] = []
    if not block:
        return out

    known_methods = {"Trực tiếp", "Trực tuyến", "Dịch vụ bưu chính"}
    cur_method: str | None = None
    cur_time: str | None = None
    order = 0

    i = 0
    while i < len(block):
        p = block[i].strip()
        # Skip table header
        if p in _FEE_HEADER_CELLS:
            i += 1
            continue

        # Detect new method row: a known method label followed by processing time
        if p in known_methods:
            cur_method = p
            # Next non-header, non-fee paragraph is the processing time
            if i + 1 < len(block):
                cand = block[i + 1].strip()
                if not cand.startswith("Phí") and cand not in _FEE_HEADER_CELLS:
                    cur_time = cand
                    i += 2
                    continue
            i += 1
            continue

        # Fee line
        if p.startswith("Phí") and cur_method:
            # Format: "Phí : 5 triệu Đồng (description...)"
            m = re.match(r"^Phí\s*:\s*([^\(]+?)(?:\s*\((.*)\))?\s*$", p, re.DOTALL)
            if m:
                amount = m.group(1).strip()
                desc = (m.group(2) or "").strip() or None
            else:
                amount = p.replace("Phí", "").lstrip(":").strip()
                desc = None
            out.append({
                "submission_method": cur_method,
                "processing_time": cur_time,
                "amount_text": amount,
                "description": desc,
                "order": order,
            })
            order += 1
            i += 1
            continue

        # Anything else (eg. "Trực tiếp tại Bộ phận Một cửa") → skip
        i += 1

    return out


_QUANTITY_RE = re.compile(r"Bản\s*chính.*Bản\s*sao", re.IGNORECASE)
_ITEM_PREFIX_RE = re.compile(r"^\d+[\)\.]\s*")


def _parse_requirements(block: list[str]) -> list[dict[str, Any]]:
    """
    State-machine parser cho section "Thành phần hồ sơ".
    Hai biến thể format cần handle:

    Format A (vd 1.015028):
        * Đối với <case_group>          ← case_group có prefix '* '
        Tên giấy tờ | Mẫu đơn | Số lượng
        1) <document>                    ← item có numbering "1)"
        <form_name>
        Bản chính: X Bản sao: Y
        ...

    Format B (vd 3.000001):
        <case_group>                     ← KHÔNG có prefix, chỉ plain text
        Tên giấy tờ | Mẫu đơn | Số lượng
        <document>                       ← item KHÔNG có numbering
        <form_name>                      (optional)
        Bản chính: X Bản sao: Y
        ...

    Thuật toán:
    - Duyệt từng paragraph, bỏ qua header cells (Tên giấy tờ/Mẫu đơn/Số lượng).
    - Buffer các line vào `buf`. Khi gặp:
        * "Tên giấy tờ"        → buf hiện tại chính là case_group cho nhóm sau
        * line khớp "Bản chính... Bản sao..." → flush 1 item:
              name = buf[0], form_name = buf[1] nếu có
    """
    out: list[dict[str, Any]] = []
    buf: list[str] = []
    case_group: str | None = None

    def _flush_item(quantity: str) -> None:
        if not buf:
            return
        raw_name = buf[0].strip()
        name = _ITEM_PREFIX_RE.sub("", raw_name).rstrip(";.,").strip()
        form_name = buf[1].strip() if len(buf) >= 2 else None
        # Nếu buf > 2 phần tử (item name multi-line), gom tail làm form_name fallback
        if not form_name and len(buf) > 2:
            form_name = " ".join(buf[1:]).strip()
        out.append({
            "name": name,
            "case_group": case_group,
            "form_name": form_name,
            "quantity": quantity.strip(),
        })

    for p in block:
        s = p.strip()
        if not s:
            continue

        if s == "Tên giấy tờ":
            # buffer hiện tại là case_group của nhóm tiếp theo
            if buf:
                cg = " ".join(buf).strip().lstrip("* ").strip()
                case_group = cg or case_group
                buf = []
            continue

        # Bỏ qua header cells còn lại
        if s in _REQ_HEADER_CELLS:
            continue

        # Dòng "Bản chính: X Bản sao: Y" → flush 1 item
        if _QUANTITY_RE.search(s):
            _flush_item(s)
            buf = []
            continue

        # Mọi dòng khác → push vào buffer
        buf.append(s)

    # Cuối block có thể còn buf chưa flush (case_group dangling) → bỏ
    return out


def _parse_legal_basis(block: list[str]) -> list[dict[str, Any]]:
    """
    Block "Căn cứ pháp lý:":
      Số ký hiệu | Trích yếu | Ngày ban hành | Cơ quan ban hành   ← header
      <code>
      <title>
      <date>
      <issuer>
      <code>
      ...
    Đôi khi 'issuer' bị thiếu cho row đầu — ta vẫn cố gắng pair theo group 4.
    """
    out: list[dict[str, Any]] = []
    filtered = [
        b.strip() for b in block
        if b.strip() not in _LEGAL_HEADER_CELLS
        and b.strip() != SECT_LEGAL.rstrip(":")
        and not b.strip().endswith(":")
    ]

    # Heuristic: row starts when we see a token like "<digits>/<year>/<...>" (e.g. "135/2015/NĐ-CP")
    code_re = re.compile(r"^\d+/\d{4}/[\w-]+|^\d+/QĐ|^QĐ/")
    rows: list[list[str]] = []
    cur: list[str] = []
    for p in filtered:
        if code_re.match(p):
            if cur:
                rows.append(cur)
            cur = [p]
        else:
            if cur:
                cur.append(p)
    if cur:
        rows.append(cur)

    for r in rows:
        # Expected order: code, title, date, issuer (issuer may be missing)
        code = r[0] if len(r) > 0 else None
        title = r[1] if len(r) > 1 else None
        date = r[2] if len(r) > 2 else None
        issuer = r[3] if len(r) > 3 else None
        out.append({
            "code": code,
            "title": title,
            "issue_date": date,
            "issuer": issuer,
        })
    return out


def _summarise_fees(fees: list[dict[str, Any]]) -> str | None:
    """Build short denormalized summary for Procedure.fee column (≤500 chars)."""
    if not fees:
        return None
    # Unique amount_text values, in order of first appearance
    seen = []
    for f in fees:
        amt = f.get("amount_text")
        if amt and amt not in seen:
            seen.append(amt)
    return "; ".join(seen)[:500] or None


def _format_legal_basis(items: list[dict[str, Any]]) -> str | None:
    if not items:
        return None
    lines = []
    for it in items:
        code = it.get("code") or ""
        title = it.get("title") or ""
        date = it.get("issue_date") or ""
        issuer = it.get("issuer") or ""
        lines.append(f"- {code}: {title} ({date}, {issuer})".strip())
    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

def parse_docx(docx_bytes: bytes) -> dict[str, Any]:
    """Parse downloaded .docx bytes into a structured dict (see module docstring)."""
    paragraphs = _extract_paragraphs(docx_bytes)

    # Locate section boundaries
    def _find(label: str) -> int:
        for i, p in enumerate(paragraphs):
            if p.strip() == label or p.strip().startswith(label):
                return i
        return -1

    idx_steps  = _find(SECT_STEPS)
    idx_method = _find(SECT_METHOD)
    idx_reqs   = _find(SECT_REQS)
    idx_legal  = _find(SECT_LEGAL)

    # ── Header (before SECT_STEPS) ────────────────────────────────────────────
    header_block = paragraphs[: idx_steps if idx_steps >= 0 else len(paragraphs)]
    data: dict[str, Any] = {}
    for label, key in _HEADER_LABELS.items():
        data[key] = _clean(_kv_after(header_block, label))

    # ── Steps (between SECT_STEPS and SECT_METHOD) ────────────────────────────
    if idx_steps >= 0 and idx_method > idx_steps:
        steps_block = paragraphs[idx_steps + 1: idx_method]
    else:
        steps_block = []
    # Drop the header label itself; join paragraphs with newline
    steps_text = "\n".join(p.strip() for p in steps_block if p.strip()).strip() or None
    data["steps_text"] = steps_text

    # ── Fees (between SECT_METHOD and SECT_REQS) ──────────────────────────────
    if idx_method >= 0 and idx_reqs > idx_method:
        fee_block = paragraphs[idx_method + 1: idx_reqs]
    else:
        fee_block = []
    data["fees"] = _parse_fees(fee_block)

    # ── Requirements (between SECT_REQS and the first meta label) ─────────────
    # Find first meta-label paragraph after idx_reqs
    end_reqs = len(paragraphs)
    if idx_reqs >= 0:
        for i in range(idx_reqs + 1, len(paragraphs)):
            for lbl in _META_LABELS:
                if paragraphs[i].startswith(lbl + ":"):
                    end_reqs = i
                    break
            if end_reqs != len(paragraphs):
                break
        req_block = paragraphs[idx_reqs + 1: end_reqs]
    else:
        req_block = []
    data["requirements"] = _parse_requirements(req_block)

    # ── Meta block (between end_reqs and SECT_LEGAL) ──────────────────────────
    meta_end = idx_legal if idx_legal > end_reqs else len(paragraphs)
    meta_block = paragraphs[end_reqs:meta_end]
    for label, key in _META_LABELS.items():
        data[key] = _clean(_kv_after(meta_block, label))

    # ── Legal basis (after SECT_LEGAL, until "Yêu cầu, điều kiện" appears OR end) ─
    if idx_legal >= 0:
        # Find first meta-label after legal section (conditions/keywords/description)
        legal_end = len(paragraphs)
        for i in range(idx_legal + 1, len(paragraphs)):
            for lbl in ("Yêu cầu, điều kiện thực hiện", "Từ khóa", "Mô tả"):
                if paragraphs[i].startswith(lbl + ":"):
                    legal_end = i
                    break
            if legal_end != len(paragraphs):
                break
        legal_block = paragraphs[idx_legal + 1:legal_end]
    else:
        legal_block = []
    legal_items = _parse_legal_basis(legal_block)
    data["legal_basis_items"] = legal_items
    data["legal_basis"] = _format_legal_basis(legal_items)

    # ── Re-parse meta labels that might live AFTER legal too (vd: conditions) ─
    # Some docs have "Yêu cầu, điều kiện" appear after legal table
    for label, key in _META_LABELS.items():
        if data.get(key):
            continue
        # try anywhere in the full doc as fallback
        v = _clean(_kv_after(paragraphs, label))
        if v:
            data[key] = v

    # ── Denormalized convenience fields for Procedure table ───────────────────
    data["fee_summary"] = _summarise_fees(data["fees"])
    # processing_time: first fee tier's time, or None
    data["processing_time"] = (data["fees"][0].get("processing_time")
                                if data["fees"] else None)

    logger.info(
        f"DocxParser | code={data.get('code')} "
        f"| fees={len(data['fees'])} "
        f"| reqs={len(data['requirements'])} "
        f"| legal={len(legal_items)} "
        f"| steps_chars={len(data.get('steps_text') or '')}"
    )
    return data
