# app/services/procedure/procedure_service.py
import math

from fastapi import HTTPException, status
from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.procedure import (
    Procedure,
    ProcedureFee,
    ProcedureRequirement,
    ProcedureStatus,
    ProcedureStep,
)
from app.schemas.common import PaginatedResponse
from app.schemas.procedure import (
    ProcedureCreateRequest,
    ProcedureDetail,
    ProcedureListItem,
    ProcedureSearchRequest,
    ProcedureUpdateRequest,
)


class ProcedureService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def search(
        self,
        params: ProcedureSearchRequest,
        *,
        agency_code: str | None = None,
    ) -> PaginatedResponse[ProcedureListItem]:
        """Search procedures với filter. `agency_code` (G02, H49, ...) match
        qua chain Procedure → DocumentChunk → DocumentSource.source_url."""
        query = select(Procedure)

        if params.status:
            query = query.where(Procedure.status == params.status)
        if params.domain:
            query = query.where(Procedure.domain == params.domain)
        if params.authority_level:
            query = query.where(Procedure.authority_level == params.authority_level)
        if params.q:
            like = f"%{params.q}%"
            query = query.where(
                or_(Procedure.name.ilike(like), Procedure.code.ilike(like))
            )
        if agency_code:
            from app.models.document import DocumentChunk, DocumentSource
            # Subquery: procedure_ids thuộc 1 source có source_url match.
            # DISTINCT để tránh duplicate khi 1 procedure có nhiều chunks.
            proc_ids_subq = (
                select(DocumentChunk.procedure_id)
                .join(DocumentSource, DocumentSource.id == DocumentChunk.source_id)
                .where(
                    DocumentSource.source_url == agency_code,
                    DocumentChunk.procedure_id.is_not(None),
                )
                .distinct()
            )
            query = query.where(Procedure.id.in_(proc_ids_subq))

        count_q = select(func.count()).select_from(query.subquery())
        total = (await self._db.execute(count_q)).scalar_one()

        offset = (params.page - 1) * params.page_size
        result = await self._db.execute(
            query.order_by(Procedure.name).offset(offset).limit(params.page_size)
        )
        procedures = result.scalars().all()

        return PaginatedResponse(
            items=[ProcedureListItem.model_validate(p) for p in procedures],
            total=total,
            page=params.page,
            page_size=params.page_size,
            total_pages=math.ceil(total / params.page_size) if total else 0,
        )

    async def get_by_id(self, procedure_id: str) -> ProcedureDetail:
        """Lookup theo procedure.id (UUID) hoặc procedure.code (vd "1.001612").
        FE thường dùng code trong URL → smart detect: chứa dấu "." → là code."""
        is_code = "." in procedure_id
        result = await self._db.execute(
            select(Procedure)
            .options(
                selectinload(Procedure.requirements),
                selectinload(Procedure.steps),
                selectinload(Procedure.fees),
            )
            .where(
                Procedure.code == procedure_id if is_code
                else Procedure.id == procedure_id
            )
        )
        procedure = result.scalar_one_or_none()
        if not procedure:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy thủ tục.",
            )
        return ProcedureDetail.model_validate(procedure)

    async def create(self, payload: ProcedureCreateRequest) -> ProcedureDetail:
        existing = await self._db.execute(
            select(Procedure).where(
                Procedure.code == payload.code,
                Procedure.status == ProcedureStatus.ACTIVE,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Thủ tục với mã '{payload.code}' đã tồn tại và đang có hiệu lực.",
            )

        procedure = Procedure(
            code=payload.code,
            name=payload.name,
            domain=payload.domain,
            authority_level=payload.authority_level,
            implementing_agency=payload.implementing_agency,
            coordinating_agency=payload.coordinating_agency,
            processing_time=payload.processing_time,
            fee=payload.fee,
            result=payload.result,
            legal_basis=payload.legal_basis,
            description=payload.description,
            effective_date=payload.effective_date,
            status=ProcedureStatus.DRAFT,
        )
        self._db.add(procedure)
        await self._db.flush()

        for req_data in payload.requirements:
            self._db.add(ProcedureRequirement(
                procedure_id=procedure.id,
                **req_data.model_dump(),
            ))

        for step_data in payload.steps:
            self._db.add(ProcedureStep(
                procedure_id=procedure.id,
                **step_data.model_dump(),
            ))

        await self._db.flush()
        logger.info(f"Procedure | create | id={procedure.id} code={procedure.code}")
        return await self.get_by_id(procedure.id)

    async def update(self, procedure_id: str, payload: ProcedureUpdateRequest) -> ProcedureDetail:
        result = await self._db.execute(select(Procedure).where(Procedure.id == procedure_id))
        procedure = result.scalar_one_or_none()
        if not procedure:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy thủ tục.")

        update_data = payload.model_dump(exclude_none=True)
        for field, value in update_data.items():
            setattr(procedure, field, value)

        logger.info(f"Procedure | update | id={procedure_id} fields={list(update_data.keys())}")
        return await self.get_by_id(procedure_id)

    async def publish(self, procedure_id: str) -> ProcedureDetail:
        """Activate a draft procedure."""
        result = await self._db.execute(select(Procedure).where(Procedure.id == procedure_id))
        procedure = result.scalar_one_or_none()
        if not procedure:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy thủ tục.")
        if procedure.status != ProcedureStatus.DRAFT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Chỉ có thể phê duyệt thủ tục ở trạng thái nháp.",
            )
        procedure.status = ProcedureStatus.ACTIVE
        logger.info(f"Procedure | publish | id={procedure_id}")
        return await self.get_by_id(procedure_id)

    async def create_version(self, procedure_id: str) -> ProcedureDetail:
        """
        Create a new draft version of an active procedure.
        Marks old as REPLACED, links via parent_id chain.
        """
        old_result = await self._db.execute(
            select(Procedure)
            .options(selectinload(Procedure.requirements), selectinload(Procedure.steps))
            .where(Procedure.id == procedure_id)
        )
        old = old_result.scalar_one_or_none()
        if not old:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy thủ tục.")

        new_procedure = Procedure(
            code=old.code,
            name=old.name,
            domain=old.domain,
            authority_level=old.authority_level,
            implementing_agency=old.implementing_agency,
            coordinating_agency=old.coordinating_agency,
            processing_time=old.processing_time,
            fee=old.fee,
            result=old.result,
            legal_basis=old.legal_basis,
            description=old.description,
            version=old.version + 1,
            parent_id=old.id,
            status=ProcedureStatus.DRAFT,
        )
        self._db.add(new_procedure)
        await self._db.flush()

        for req in old.requirements:
            self._db.add(ProcedureRequirement(
                procedure_id=new_procedure.id,
                name=req.name,
                form_name=req.form_name,
                quantity=req.quantity,
                document_type=req.document_type,
                note=req.note,
                is_mandatory=req.is_mandatory,
                order=req.order,
            ))
        for step in old.steps:
            self._db.add(ProcedureStep(
                procedure_id=new_procedure.id,
                step_order=step.step_order,
                title=step.title,
                description=step.description,
                responsible_party=step.responsible_party,
                duration=step.duration,
            ))

        old.status = ProcedureStatus.REPLACED
        old.replaced_by = new_procedure.id
        await self._db.flush()

        logger.info(f"Procedure | new_version | old={procedure_id} new={new_procedure.id} v={new_procedure.version}")
        return await self.get_by_id(new_procedure.id)
