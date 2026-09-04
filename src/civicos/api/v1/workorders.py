"""Work order and crew endpoints - the dispatch board."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select

from civicos.api.deps import (
    CurrentUserDep,
    PageDep,
    SessionDep,
    TenantDep,
    require_permission,
)
from civicos.core.errors import NotFoundError
from civicos.core.pagination import Page as PageResult
from civicos.core.permissions import Resource, perm
from civicos.domain.enums import Priority, WorkOrderStatus, WorkOrderType
from civicos.domain.workorders import Crew, CrewMember, WorkOrder
from civicos.schemas.common import Message, Page
from civicos.schemas.operations import (
    CrewCreateRequest,
    CrewOut,
    WorkOrderCompleteRequest,
    WorkOrderCreateRequest,
    WorkOrderDetail,
    WorkOrderDispatchRequest,
    WorkOrderSummary,
    WorkOrderTransitionRequest,
    WorkOrderUpdateOut,
    WorkOrderUpdateRequest,
)
from civicos.services import workorder_service

router = APIRouter(prefix="/work-orders", tags=["Work Orders"])

READ = perm(Resource.WORK_ORDER, "read")
CREATE = perm(Resource.WORK_ORDER, "create")


@router.post(
    "",
    response_model=WorkOrderDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(CREATE))],
)
async def create_work_order(
    payload: WorkOrderCreateRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> WorkOrderDetail:
    draft = workorder_service.WorkOrderDraft(
        title=payload.title,
        instructions=payload.instructions,
        order_type=payload.order_type,
        priority=payload.priority,
        issue_id=payload.issue_id,
        asset_id=payload.asset_id,
        department_id=payload.department_id,
        admin_unit_id=payload.admin_unit_id,
        crew_id=payload.crew_id,
        assigned_to_id=payload.assigned_to_id,
        scheduled_for=payload.scheduled_for,
        due_at=payload.due_at,
        estimated_hours=payload.estimated_hours,
        estimated_cost=payload.estimated_cost,
        latitude=payload.latitude,
        longitude=payload.longitude,
        address=payload.address,
        checklist=[item.model_dump() for item in payload.checklist],
        requires_verification=payload.requires_verification,
    )
    order = await workorder_service.create_work_order(
        session, tenant, draft, auto_dispatch=payload.auto_dispatch, actor=user
    )
    await session.commit()
    return WorkOrderDetail.model_validate(order)


@router.get(
    "", response_model=Page[WorkOrderSummary], dependencies=[Depends(require_permission(READ))]
)
async def list_work_orders(
    session: SessionDep,
    tenant: TenantDep,
    page: PageDep,
    status_filter: Annotated[list[WorkOrderStatus] | None, Query(alias="status")] = None,
    order_type: Annotated[WorkOrderType | None, Query()] = None,
    priority: Annotated[Priority | None, Query()] = None,
    crew_id: Annotated[uuid.UUID | None, Query()] = None,
    assigned_to_id: Annotated[uuid.UUID | None, Query()] = None,
    department_id: Annotated[uuid.UUID | None, Query()] = None,
    issue_id: Annotated[uuid.UUID | None, Query()] = None,
    overdue_only: Annotated[bool, Query()] = False,
    open_only: Annotated[bool, Query()] = False,
) -> Page[WorkOrderSummary]:
    from civicos.core.clock import utcnow

    statement = select(WorkOrder).where(
        WorkOrder.tenant_id == tenant.id, WorkOrder.deleted_at.is_(None)
    )
    if status_filter:
        statement = statement.where(WorkOrder.status.in_(status_filter))
    if order_type:
        statement = statement.where(WorkOrder.order_type == order_type)
    if priority:
        statement = statement.where(WorkOrder.priority == priority)
    if crew_id:
        statement = statement.where(WorkOrder.crew_id == crew_id)
    if assigned_to_id:
        statement = statement.where(WorkOrder.assigned_to_id == assigned_to_id)
    if department_id:
        statement = statement.where(WorkOrder.department_id == department_id)
    if issue_id:
        statement = statement.where(WorkOrder.issue_id == issue_id)
    if open_only or overdue_only:
        statement = statement.where(
            WorkOrder.status.not_in(
                [
                    WorkOrderStatus.COMPLETED,
                    WorkOrderStatus.VERIFIED,
                    WorkOrderStatus.CANCELLED,
                ]
            )
        )
    if overdue_only:
        statement = statement.where(WorkOrder.due_at.is_not(None), WorkOrder.due_at < utcnow())

    total = int(await session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = (
        (
            await session.scalars(
                statement.order_by(WorkOrder.created_at.desc())
                .offset(page.offset)
                .limit(page.limit)
            )
        )
        .unique()
        .all()
    )
    result = PageResult.build([WorkOrderSummary.model_validate(row) for row in rows], total, page)
    return Page[WorkOrderSummary].model_validate(result.model_dump())


@router.get("/mine", response_model=list[WorkOrderSummary])
async def my_work_orders(
    session: SessionDep, tenant: TenantDep, user: CurrentUserDep
) -> list[WorkOrderSummary]:
    """The field worker's job list for today, soonest deadline first."""
    rows = (
        (
            await session.scalars(
                select(WorkOrder)
                .where(
                    WorkOrder.tenant_id == tenant.id,
                    WorkOrder.deleted_at.is_(None),
                    WorkOrder.assigned_to_id == user.id,
                    WorkOrder.status.not_in(
                        [
                            WorkOrderStatus.COMPLETED,
                            WorkOrderStatus.VERIFIED,
                            WorkOrderStatus.CANCELLED,
                        ]
                    ),
                )
                .order_by(WorkOrder.due_at.asc().nullslast(), WorkOrder.priority.desc())
            )
        )
        .unique()
        .all()
    )
    return [WorkOrderSummary.model_validate(row) for row in rows]


@router.get(
    "/{order_id}", response_model=WorkOrderDetail, dependencies=[Depends(require_permission(READ))]
)
async def get_work_order(
    order_id: uuid.UUID, session: SessionDep, tenant: TenantDep
) -> WorkOrderDetail:
    return WorkOrderDetail.model_validate(await _load(session, tenant.id, order_id))


@router.post(
    "/{order_id}/dispatch",
    response_model=WorkOrderDetail,
    dependencies=[Depends(require_permission(perm(Resource.WORK_ORDER, "assign")))],
)
async def dispatch_work_order(
    order_id: uuid.UUID,
    payload: WorkOrderDispatchRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> WorkOrderDetail:
    order = await _load(session, tenant.id, order_id)
    await workorder_service.dispatch(
        session,
        tenant,
        order,
        crew_id=payload.crew_id,
        assignee_id=payload.assignee_id,
        actor=user,
    )
    await session.commit()
    return WorkOrderDetail.model_validate(order)


@router.post(
    "/{order_id}/transition",
    response_model=WorkOrderDetail,
    dependencies=[Depends(require_permission(perm(Resource.WORK_ORDER, "update")))],
)
async def transition_work_order(
    order_id: uuid.UUID,
    payload: WorkOrderTransitionRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> WorkOrderDetail:
    order = await _load(session, tenant.id, order_id)
    await workorder_service.transition(
        session, tenant, order, payload.status, note=payload.note, actor=user
    )
    await session.commit()
    return WorkOrderDetail.model_validate(order)


@router.post(
    "/{order_id}/updates",
    response_model=WorkOrderUpdateOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(Resource.WORK_ORDER, "update_assigned")))],
)
async def add_update(
    order_id: uuid.UUID,
    payload: WorkOrderUpdateRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> WorkOrderUpdateOut:
    """Post a progress note from the field. Safe to submit after offline work."""
    order = await _load(session, tenant.id, order_id)
    update = await workorder_service.record_update(
        session,
        tenant,
        order,
        note=payload.note,
        latitude=payload.latitude,
        longitude=payload.longitude,
        recorded_at=payload.recorded_at,
        checklist=[item.model_dump() for item in payload.checklist]
        if payload.checklist is not None
        else None,
        actor=user,
    )
    await session.commit()
    return WorkOrderUpdateOut.model_validate(update)


@router.post(
    "/{order_id}/complete",
    response_model=WorkOrderDetail,
    dependencies=[Depends(require_permission(perm(Resource.WORK_ORDER, "complete")))],
)
async def complete_work_order(
    order_id: uuid.UUID,
    payload: WorkOrderCompleteRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> WorkOrderDetail:
    """Close out a job. Resolves the originating report if nothing else is open."""
    order = await _load(session, tenant.id, order_id)
    await workorder_service.complete(
        session,
        tenant,
        order,
        note=payload.note,
        actual_hours=payload.actual_hours,
        actual_cost=payload.actual_cost,
        materials=[material.model_dump() for material in payload.materials],
        signoff_name=payload.signoff_name,
        asset_condition=payload.asset_condition,
        actor=user,
    )
    await session.commit()
    return WorkOrderDetail.model_validate(order)


# ------------------------------------------------------------------- crews ---

crews_router = APIRouter(prefix="/crews", tags=["Work Orders"])


@router.get(
    "/crews/{crew_id}/workload",
    response_model=dict,
    dependencies=[Depends(require_permission(READ))],
)
async def crew_workload(crew_id: uuid.UUID, session: SessionDep, tenant: TenantDep) -> dict:
    return await workorder_service.crew_workload(session, tenant.id, crew_id)


@crews_router.post(
    "",
    response_model=CrewOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(CREATE))],
)
async def create_crew(
    payload: CrewCreateRequest, session: SessionDep, tenant: TenantDep
) -> CrewOut:
    crew = Crew(
        tenant_id=tenant.id,
        code=payload.code,
        name=payload.name,
        department_id=payload.department_id,
        supervisor_id=payload.supervisor_id,
        shift=payload.shift,
        coverage_unit_ids=[str(unit) for unit in payload.coverage_unit_ids],
        skills=payload.skills,
        vehicle_registration=payload.vehicle_registration,
        contact_phone=payload.contact_phone,
        capacity_per_day=payload.capacity_per_day,
    )
    session.add(crew)
    await session.flush()
    for member_id in payload.member_ids:
        session.add(CrewMember(tenant_id=tenant.id, crew_id=crew.id, user_id=member_id))
    await session.commit()
    return CrewOut.model_validate(crew)


@crews_router.get(
    "", response_model=list[CrewOut], dependencies=[Depends(require_permission(READ))]
)
async def list_crews(
    session: SessionDep,
    tenant: TenantDep,
    department_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[CrewOut]:
    statement = select(Crew).where(Crew.tenant_id == tenant.id, Crew.is_active.is_(True))
    if department_id:
        statement = statement.where(Crew.department_id == department_id)
    rows = (await session.scalars(statement.order_by(Crew.name))).unique().all()
    return [CrewOut.model_validate(row) for row in rows]


@crews_router.delete(
    "/{crew_id}",
    response_model=Message,
    dependencies=[Depends(require_permission(perm(Resource.WORK_ORDER, "update")))],
)
async def deactivate_crew(crew_id: uuid.UUID, session: SessionDep, tenant: TenantDep) -> Message:
    crew = await session.get(Crew, crew_id)
    if crew is None or crew.tenant_id != tenant.id:
        raise NotFoundError("Crew not found.", code="crew_not_found")
    crew.is_active = False
    await session.commit()
    return Message(message=f"Crew '{crew.name}' deactivated.")


async def _load(session, tenant_id: uuid.UUID, order_id: uuid.UUID) -> WorkOrder:
    from sqlalchemy.orm import selectinload

    order = await session.scalar(
        select(WorkOrder)
        .where(
            WorkOrder.id == order_id,
            WorkOrder.tenant_id == tenant_id,
            WorkOrder.deleted_at.is_(None),
        )
        .options(selectinload(WorkOrder.updates), selectinload(WorkOrder.materials))
    )
    if order is None:
        raise NotFoundError("Work order not found.", code="work_order_not_found")
    return order
