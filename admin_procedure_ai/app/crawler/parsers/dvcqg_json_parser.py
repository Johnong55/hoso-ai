"""
Parser cho response JSON detail của API DVCQG mới:

  POST https://dichvucong.gov.vn/api/v1/configuring/formality/get-formality-by-citizen
  Body: {"id": "<UUID>"}

Mục tiêu: trả về dict có shape KHỚP với `ProcedureChunker.chunk_procedure` và
`_process_parsed_procedure` (worker/tasks.py) — tức là cùng các key mà bản
docx_parser cũ trả ra. Nhờ vậy phần downstream (chunk + embed + persist)
không cần biết đang dùng JSON hay docx.

Output dict keys:
    code, name, domain, description, conditions, result,
    legal_basis, legal_basis_items,
    implementing_agency, coordinating_agency, authority_level_text,
    steps_text,
    fees: [{submission_method, processing_time, amount_text, description, order}],
    requirements: [{name, description, case_group, form_name, form_url,
                    quantity, is_mandatory, document_type, note}],
    fee_summary, processing_time,
    source_updated_at,        # epoch ms từ API.updatedAt
    source_id,                # UUID gốc bên DVCQG (≠ procedure.id nội bộ)
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote


# ── Map submissionMethod sang tên tiếng Việt nhất quán với DB cũ ──────────────
_METHOD_MAP = {
    "DIRECT": "Trực tiếp",
    "ONLINE": "Trực tuyến",
    "POSTAL": "Dịch vụ bưu chính",
    "DIRECT_AND_ONLINE": "Trực tiếp và Trực tuyến",
}

# Map processingTimeUnit sang tiếng Việt
_TIME_UNIT_MAP = {
    "WORKING_DAY": "Ngày làm việc",
    "DAY": "Ngày",
    "HOUR": "Giờ",
    "MONTH": "Tháng",
    "YEAR": "Năm",
}

# Path tới proxy backend GET (frontend prepends API base origin).
# Backend (forms.py) sẽ POST hộ sang DVCQG /preview-attachment.
_PROXY_PATH = "/api/v1/forms/{id}"


def _build_form_url(attachment_id: str, file_name: str | None = None) -> str:
    """
    Trả về URL proxy backend tải biểu mẫu. Browser GET URL này thay vì
    GET trực tiếp DVCQG (DVCQG từ chối GET, đòi POST với body fileId).

    Đính kèm `?name=<filename>` để browser lưu đúng tên gốc (RFC 5987).
    """
    url = _PROXY_PATH.format(id=attachment_id)
    if file_name:
        url += f"?name={quote(file_name)}"
    return url


def _norm_method(raw: str | None) -> str:
    if not raw:
        return "Khác"
    return _METHOD_MAP.get(raw, raw)


def _format_processing_time(qty: Any, unit: str | None) -> str | None:
    if qty is None and not unit:
        return None
    parts: list[str] = []
    if qty is not None and str(qty).strip() != "":
        parts.append(str(qty).strip())
    if unit:
        parts.append(_TIME_UNIT_MAP.get(unit, unit))
    return " ".join(parts).strip() or None


def _format_amount(value: Any, currency_name: str | None) -> str | None:
    if value is None and not currency_name:
        return None
    # value có thể là int/float/str
    if value is None:
        return currency_name
    try:
        f = float(value)
        if f == 0:
            amt_str = "0"
        elif f.is_integer():
            amt_str = f"{int(f):,}".replace(",", ".")
        else:
            amt_str = f"{f:,.2f}".replace(",", ".")
    except (TypeError, ValueError):
        amt_str = str(value)
    if currency_name:
        return f"{amt_str} {currency_name}".strip()
    return amt_str


def _format_legal_basis(items: list[dict[str, Any]]) -> str | None:
    """Format legalBasisesDetails → text với mỗi item là `- {code}: {name}`."""
    if not items:
        return None
    lines: list[str] = []
    for it in items:
        code = (it.get("code") or "").strip()
        name = (it.get("name") or "").strip()
        if code and name:
            lines.append(f"- {code}: {name}")
        elif name:
            lines.append(f"- {name}")
        elif code:
            lines.append(f"- {code}")
    return "\n".join(lines) or None


def _flatten_fees(execution_methods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Flatten executionMethods[].fees[] → list dict cho ProcedureFee.
    Mỗi (method × fee tier) → 1 row. Method không có fee → 1 row amount_text=None.
    """
    out: list[dict[str, Any]] = []
    order = 0
    for m in execution_methods or []:
        method = _norm_method(m.get("submissionMethod"))
        ptime = _format_processing_time(
            m.get("processingTime"), m.get("processingTimeUnit")
        )
        method_desc = (m.get("description") or "").strip() or None

        fees = m.get("fees") or []
        if not fees:
            out.append({
                "submission_method": method,
                "processing_time": ptime,
                "amount_text": None,
                "description": method_desc,
                "order": order,
            })
            order += 1
            continue

        for fee in fees:
            amount = _format_amount(fee.get("value"), fee.get("currencyName"))
            fee_desc = (fee.get("description") or "").strip() or None
            # Ưu tiên description của fee tier; nếu không có, dùng description của method
            desc = fee_desc or method_desc
            out.append({
                "submission_method": method,
                "processing_time": ptime,
                "amount_text": amount,
                "description": desc,
                "order": order,
            })
            order += 1
    return out


def _summarise_fees(fees: list[dict[str, Any]]) -> str | None:
    """Compact summary cho Procedure.fee denorm: unique amount_text, joined."""
    if not fees:
        return None
    seen: list[str] = []
    for f in fees:
        a = (f.get("amount_text") or "").strip()
        if a and a not in seen:
            seen.append(a)
    if not seen:
        return None
    return "; ".join(seen)[:500] or None


def _flatten_requirements(execution_cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Flatten executionCases[].profileComponents[] → list dict cho ProcedureRequirement.
    case_group = executionCase.name (vd "Giấy tờ phải nộp"). Mỗi profileComponent → 1 row.
    Lấy attachments[0] làm form_name/form_url (nếu có).
    """
    out: list[dict[str, Any]] = []
    for case in execution_cases or []:
        case_group = (case.get("name") or "").strip() or "Bao gồm"
        for pc in case.get("profileComponents") or []:
            name = (pc.get("name") or "").strip()
            if not name:
                continue

            original_qty = pc.get("originalQty")
            copy_qty = pc.get("copyQty")
            qty_parts: list[str] = []
            if original_qty not in (None, 0, "0"):
                qty_parts.append(f"Bản chính: {original_qty}")
            if copy_qty not in (None, 0, "0"):
                qty_parts.append(f"Bản sao: {copy_qty}")
            quantity = " ".join(qty_parts) or None

            attachments = pc.get("attachments") or []
            form_name = None
            form_url = None
            if attachments:
                first = attachments[0] or {}
                form_name = (first.get("fileName") or "").strip() or None
                att_id = (first.get("id") or "").strip()
                if att_id:
                    form_url = _build_form_url(att_id, file_name=form_name)

            out.append({
                "name": name,
                "description": (pc.get("description") or "").strip() or None,
                "case_group": case_group,
                "form_name": form_name,
                "form_url": form_url,
                "quantity": quantity,
                "document_type": (pc.get("code") or "").strip() or None,
                "is_mandatory": bool(pc.get("required", True)),
                "note": None,
            })
    return out


def _steps_text(execution_steps: list[dict[str, Any]]) -> str | None:
    """Gộp executionSteps → 1 blob text. Mỗi step: '<name>: <description>'."""
    if not execution_steps:
        return None
    lines: list[str] = []
    for s in execution_steps:
        name = (s.get("name") or "").strip()
        desc = (s.get("description") or "").strip()
        if name and desc:
            lines.append(f"{name}: {desc}")
        elif desc:
            lines.append(desc)
        elif name:
            lines.append(name)
    return "\n".join(lines).strip() or None


def _join_names(items: list[dict[str, Any]], sep: str = ", ") -> str | None:
    if not items:
        return None
    names = [(it.get("name") or "").strip() for it in items]
    names = [n for n in names if n]
    return sep.join(names) or None


def parse_formality_json(data: dict[str, Any]) -> dict[str, Any]:
    """
    Map response JSON detail → dict shape khớp với chunker + tasks.

    `data` chính là `response.data` của
    POST /configuring/formality/get-formality-by-citizen.
    """
    if not isinstance(data, dict):
        raise ValueError("parse_formality_json: data must be a dict")

    execution_methods = data.get("executionMethods") or []
    execution_cases = data.get("executionCases") or []
    execution_steps = data.get("executionSteps") or []
    legal_items = data.get("legalBasisesDetails") or []
    categories = data.get("categoriesDetails") or []
    departments = data.get("departmentsExecuting") or []
    results = data.get("resultsDetails") or []

    fees = _flatten_fees(execution_methods)
    requirements = _flatten_requirements(execution_cases)

    # Processing time denorm: lấy method đầu tiên có processingTime
    proc_time_summary: str | None = None
    for m in execution_methods:
        pt = _format_processing_time(
            m.get("processingTime"), m.get("processingTimeUnit")
        )
        if pt:
            proc_time_summary = pt
            break

    # Authority level từ flags
    if data.get("isMinistry"):
        authority_level_text = "Cấp Bộ"
    elif data.get("isProvince"):
        authority_level_text = "Cấp Tỉnh"
    elif data.get("isWard"):
        authority_level_text = "Cấp Xã"
    elif data.get("isInternal"):
        authority_level_text = "Cấp Trung ương"
    else:
        authority_level_text = None

    parsed: dict[str, Any] = {
        "code": (data.get("code") or data.get("codeNotation") or "").strip(),
        "name": (data.get("name") or "").strip(),
        "domain": _join_names(categories, sep="; "),
        "description": (data.get("description") or "").strip() or None,
        "conditions": (data.get("requirementsAndConditions") or "").strip() or None,
        "result": _join_names(results),
        "legal_basis": _format_legal_basis(legal_items),
        "legal_basis_items": [
            {
                "code": (it.get("code") or "").strip() or None,
                "name": (it.get("name") or "").strip() or None,
                "id": it.get("id"),
            }
            for it in legal_items
        ],
        "implementing_agency": _join_names(departments, sep="; "),
        "coordinating_agency": None,
        "authority_level_text": authority_level_text,
        "decision_number": (data.get("decisionNo") or "").strip() or None,
        "object": None,  # subjectTypesDetails — bỏ qua, dùng cho stats nếu cần sau
        "steps_text": _steps_text(execution_steps),
        "fees": fees,
        "requirements": requirements,
        "fee_summary": _summarise_fees(fees),
        "processing_time": proc_time_summary,
        # change-detection fields
        "source_updated_at": data.get("updatedAt"),
        "source_id": data.get("id"),
    }
    return parsed
