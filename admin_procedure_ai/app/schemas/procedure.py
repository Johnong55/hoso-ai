# app/schemas/procedure.py
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.procedure import AuthorityLevel, ProcedureStatus


class RequirementSchema(BaseModel):
    id: str
    name: str
    description: str | None  # DD: mô tả chi tiết (bản gốc/sao, công chứng, số lượng...)
    form_name: str | None
    quantity: str | None
    document_type: str | None
    note: str | None
    is_mandatory: bool
    order: int

    model_config = {"from_attributes": True}


class StepSchema(BaseModel):
    id: str
    step_order: int  # DD: step_order (was: order — tránh xung đột SQL keyword)
    title: str
    description: str | None
    responsible_party: str | None
    duration: str | None

    model_config = {"from_attributes": True}


class ProcedureListItem(BaseModel):
    id: str
    code: str
    name: str
    domain: str
    authority_level: AuthorityLevel
    implementing_agency: str | None
    processing_time: str | None
    fee: str | None
    status: ProcedureStatus
    version: int
    effective_date: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ProcedureDetail(ProcedureListItem):
    description: str | None
    legal_basis: str | None
    result: str | None
    coordinating_agency: str | None
    requirements: list[RequirementSchema] = []
    steps: list[StepSchema] = []
    parent_id: str | None
    replaced_by: str | None
    expired_date: datetime | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class RequirementCreateRequest(BaseModel):
    name: str = Field(..., min_length=3, max_length=500)
    form_name: str | None = None
    quantity: str | None = None
    document_type: str | None = None
    note: str | None = None
    is_mandatory: bool = True
    order: int = 0


class StepCreateRequest(BaseModel):
    step_order: int = Field(..., ge=1)  # DD: step_order
    title: str = Field(..., min_length=3, max_length=500)
    description: str | None = None
    responsible_party: str | None = None
    duration: str | None = None


class ProcedureCreateRequest(BaseModel):
    code: str = Field(..., min_length=2, max_length=100)
    name: str = Field(..., min_length=5, max_length=500)
    domain: str = Field(..., min_length=2, max_length=200)
    authority_level: AuthorityLevel
    implementing_agency: str | None = None
    coordinating_agency: str | None = None
    processing_time: str | None = None
    fee: str | None = None
    result: str | None = None
    legal_basis: str | None = None
    description: str | None = None
    effective_date: datetime | None = None
    requirements: list[RequirementCreateRequest] = []
    steps: list[StepCreateRequest] = []


class ProcedureUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=5, max_length=500)
    domain: str | None = Field(None, min_length=2, max_length=200)
    implementing_agency: str | None = None
    coordinating_agency: str | None = None
    processing_time: str | None = None
    fee: str | None = None
    result: str | None = None
    legal_basis: str | None = None
    description: str | None = None
    effective_date: datetime | None = None
    status: ProcedureStatus | None = None


class ProcedureSearchRequest(BaseModel):
    q: str | None = Field(None, max_length=300)
    domain: str | None = None
    authority_level: AuthorityLevel | None = None
    locality: str | None = None
    status: ProcedureStatus | None = ProcedureStatus.ACTIVE
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
