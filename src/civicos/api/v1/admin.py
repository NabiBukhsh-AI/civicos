"""Administration: municipality configuration, org structure, taxonomy, SLAs.

This is the surface that makes CivicOS adaptable rather than bespoke: a new
municipality is onboarded, and an existing one reshapes its departments,
categories and service targets, entirely through these endpoints.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select

from civicos.api.deps import (
    PageDep,
    SessionDep,
    TenantDep,
    require_permission,
    require_superadmin,
)
from civicos.core.errors import ConflictError, NotFoundError
from civicos.core.pagination import Page as PageResult
from civicos.core.permissions import Resource, perm
from civicos.core.text import slugify
from civicos.domain.enums import AuditAction
from civicos.domain.identity import User
from civicos.domain.operations import AuditLog
from civicos.domain.tenancy import (
    AdminUnit,
    Department,
    IssueCategory,
    Municipality,
    Representative,
    SLAPolicy,
)
from civicos.schemas.auth import StaffUpdateRequest, UserOut
from civicos.schemas.common import Message, Page
from civicos.schemas.tenancy import (
    AdminUnitCreateRequest,
    AdminUnitOut,
    CategoryCreateRequest,
    CategoryOut,
    CategoryUpdateRequest,
    DepartmentCreateRequest,
    DepartmentOut,
    MunicipalityCreateRequest,
    MunicipalityOut,
    MunicipalityUpdateRequest,
    RepresentativeOut,
    RepresentativeRequest,
    SLAPolicyOut,
    SLAPolicyRequest,
)
from civicos.services import audit_service

router = APIRouter(prefix="/admin", tags=["Administration"])

MANAGE_TENANT = perm(Resource.TENANT, "update")
MANAGE_CATEGORIES = perm(Resource.CATEGORY, "*")
MANAGE_USERS = perm(Resource.USER, "*")


# ---------------------------------------------------------- municipalities ---


@router.post(
    "/municipalities",
    response_model=MunicipalityOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_superadmin())],
)
async def create_municipality(
    payload: MunicipalityCreateRequest, session: SessionDep
) -> MunicipalityOut:
    """Onboard a new municipality. Platform administrators only."""
    from civicos.db.seed import seed_tenant_defaults

    slug = slugify(payload.slug)
    if await session.scalar(select(Municipality).where(Municipality.slug == slug)):
        raise ConflictError(
            f"A municipality with the identifier '{slug}' already exists.",
            code="tenant_exists",
        )

    tenant = Municipality(
        slug=slug,
        name=payload.name,
        local_name=payload.local_name,
        tier=payload.tier,
        parent_authority=payload.parent_authority,
        country_code=payload.country_code.upper(),
        region=payload.region,
        timezone=payload.timezone,
        currency_code=payload.currency_code.upper(),
        default_language=payload.default_language,
        supported_languages=payload.supported_languages,
        centre_latitude=payload.centre_latitude,
        centre_longitude=payload.centre_longitude,
        population=payload.population,
    )
    session.add(tenant)
    await session.flush()

    if payload.seed_defaults:
        await seed_tenant_defaults(session, tenant)

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        entity_type="municipality",
        entity_id=tenant.id,
        entity_label=tenant.slug,
        summary=f"Municipality '{tenant.name}' onboarded",
        tenant_id=tenant.id,
    )
    await session.commit()
    return MunicipalityOut.model_validate(tenant)


@router.get("/municipality", response_model=MunicipalityOut)
async def get_municipality(tenant: TenantDep) -> MunicipalityOut:
    return MunicipalityOut.model_validate(tenant)


@router.patch(
    "/municipality",
    response_model=MunicipalityOut,
    dependencies=[Depends(require_permission(MANAGE_TENANT))],
)
async def update_municipality(
    payload: MunicipalityUpdateRequest, session: SessionDep, tenant: TenantDep
) -> MunicipalityOut:
    """Update branding, contact details, map defaults and feature switches."""
    fields = payload.model_dump(exclude_unset=True)
    before = audit_service.snapshot(tenant, tuple(fields))
    for field, value in fields.items():
        if field == "features" and value is not None:
            tenant.features = {**tenant.features, **value}
        elif field == "settings" and value is not None:
            tenant.settings = {**tenant.settings, **value}
        else:
            setattr(tenant, field, value)
    await audit_service.record_change(
        session,
        entity_type="municipality",
        entity_id=tenant.id,
        entity_label=tenant.slug,
        before=before,
        after=audit_service.snapshot(tenant, tuple(fields)),
    )
    await session.commit()
    return MunicipalityOut.model_validate(tenant)


# ------------------------------------------------------------- admin units ---


@router.post(
    "/admin-units",
    response_model=AdminUnitOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(MANAGE_TENANT))],
)
async def create_admin_unit(
    payload: AdminUnitCreateRequest, session: SessionDep, tenant: TenantDep
) -> AdminUnitOut:
    """Add a ward, union council or neighbourhood."""
    path = f"/{slugify(payload.name)}"
    if payload.parent_id:
        parent = await session.get(AdminUnit, payload.parent_id)
        if parent is None or parent.tenant_id != tenant.id:
            raise NotFoundError("Parent unit not found.", code="admin_unit_not_found")
        path = f"{parent.path}{path}"

    unit = AdminUnit(
        tenant_id=tenant.id,
        code=payload.code,
        name=payload.name,
        local_name=payload.local_name,
        unit_type=payload.unit_type,
        parent_id=payload.parent_id,
        path=path,
        centre_latitude=payload.centre_latitude,
        centre_longitude=payload.centre_longitude,
        boundary=payload.boundary,
        population=payload.population,
    )
    session.add(unit)
    await session.commit()
    return AdminUnitOut.model_validate(unit)


@router.get("/admin-units", response_model=list[AdminUnitOut])
async def list_admin_units(session: SessionDep, tenant: TenantDep) -> list[AdminUnitOut]:
    rows = (
        await session.scalars(
            select(AdminUnit)
            .where(AdminUnit.tenant_id == tenant.id, AdminUnit.is_active.is_(True))
            .order_by(AdminUnit.path, AdminUnit.name)
        )
    ).all()
    return [AdminUnitOut.model_validate(row) for row in rows]


# ------------------------------------------------------------- departments ---


@router.post(
    "/departments",
    response_model=DepartmentOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(MANAGE_TENANT))],
)
async def create_department(
    payload: DepartmentCreateRequest, session: SessionDep, tenant: TenantDep
) -> DepartmentOut:
    department = Department(
        tenant_id=tenant.id,
        code=payload.code,
        name=payload.name,
        local_name=payload.local_name,
        description=payload.description,
        head_user_id=payload.head_user_id,
        contact_phone=payload.contact_phone,
        contact_email=payload.contact_email,
        escalation_chain=[str(item) for item in payload.escalation_chain],
        working_hours=payload.working_hours
        or {"start": "09:00", "end": "17:00", "weekend_days": [6]},
    )
    session.add(department)
    await session.commit()
    return DepartmentOut.model_validate(department)


@router.get("/departments", response_model=list[DepartmentOut])
async def list_departments(session: SessionDep, tenant: TenantDep) -> list[DepartmentOut]:
    rows = (
        await session.scalars(
            select(Department)
            .where(Department.tenant_id == tenant.id, Department.is_active.is_(True))
            .order_by(Department.name)
        )
    ).all()
    return [DepartmentOut.model_validate(row) for row in rows]


# -------------------------------------------------------------- categories ---


@router.post(
    "/categories",
    response_model=CategoryOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(MANAGE_CATEGORIES))],
)
async def create_category(
    payload: CategoryCreateRequest, session: SessionDep, tenant: TenantDep
) -> CategoryOut:
    """Add a complaint category.

    ``keywords`` and ``ai_hints`` teach the triage classifier local vocabulary
    immediately - no deployment required.
    """
    category = IssueCategory(tenant_id=tenant.id, **payload.model_dump())
    session.add(category)
    await session.commit()
    return CategoryOut.model_validate(category)


@router.patch(
    "/categories/{category_id}",
    response_model=CategoryOut,
    dependencies=[Depends(require_permission(MANAGE_CATEGORIES))],
)
async def update_category(
    category_id: uuid.UUID,
    payload: CategoryUpdateRequest,
    session: SessionDep,
    tenant: TenantDep,
) -> CategoryOut:
    category = await session.get(IssueCategory, category_id)
    if category is None or category.tenant_id != tenant.id:
        raise NotFoundError("Category not found.", code="category_not_found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(category, field, value)
    await session.commit()
    return CategoryOut.model_validate(category)


# --------------------------------------------------------------------- SLA ---


@router.post(
    "/sla-policies",
    response_model=SLAPolicyOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(Resource.SLA, "*")))],
)
async def create_sla_policy(
    payload: SLAPolicyRequest, session: SessionDep, tenant: TenantDep
) -> SLAPolicyOut:
    """Define a response/resolution target.

    Policies resolve most-specific-first, so a broad tenant default and a tight
    category-specific override can coexist.
    """
    policy = SLAPolicy(tenant_id=tenant.id, **payload.model_dump())
    session.add(policy)
    await session.commit()
    return SLAPolicyOut.model_validate(policy)


@router.get("/sla-policies", response_model=list[SLAPolicyOut])
async def list_sla_policies(session: SessionDep, tenant: TenantDep) -> list[SLAPolicyOut]:
    rows = (
        await session.scalars(
            select(SLAPolicy).where(SLAPolicy.tenant_id == tenant.id).order_by(SLAPolicy.name)
        )
    ).all()
    return [SLAPolicyOut.model_validate(row) for row in rows]


# ---------------------------------------------------------------- staffing ---


@router.get(
    "/users",
    response_model=Page[UserOut],
    dependencies=[Depends(require_permission(perm(Resource.USER, "read")))],
)
async def list_users(
    session: SessionDep,
    tenant: TenantDep,
    page: PageDep,
    role: Annotated[str | None, Query()] = None,
    department_id: Annotated[uuid.UUID | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=160)] = None,
) -> Page[UserOut]:
    statement = select(User).where(User.tenant_id == tenant.id, User.deleted_at.is_(None))
    if role:
        statement = statement.where(User.role == role)
    if department_id:
        statement = statement.where(User.department_id == department_id)
    if search:
        term = f"%{search.lower()}%"
        statement = statement.where(
            func.lower(User.full_name).like(term)
            | func.lower(func.coalesce(User.email, "")).like(term)
            | func.lower(func.coalesce(User.phone, "")).like(term)
        )

    total = int(await session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = (
        await session.scalars(
            statement.order_by(User.full_name).offset(page.offset).limit(page.limit)
        )
    ).all()
    result = PageResult.build([UserOut.model_validate(row) for row in rows], total, page)
    return Page[UserOut].model_validate(result.model_dump())


@router.patch(
    "/users/{user_id}",
    response_model=UserOut,
    dependencies=[Depends(require_permission(MANAGE_USERS))],
)
async def update_user(
    user_id: uuid.UUID,
    payload: StaffUpdateRequest,
    session: SessionDep,
    tenant: TenantDep,
) -> UserOut:
    user = await session.get(User, user_id)
    if user is None or user.tenant_id != tenant.id:
        raise NotFoundError("User not found.", code="user_not_found")
    fields = payload.model_dump(exclude_unset=True)
    before = audit_service.snapshot(user, tuple(fields))
    for field, value in fields.items():
        setattr(user, field, value)
    await audit_service.record_change(
        session,
        entity_type="user",
        entity_id=user.id,
        entity_label=user.identifier,
        before=before,
        after=audit_service.snapshot(user, tuple(fields)),
    )
    await session.commit()
    return UserOut.model_validate(user)


# --------------------------------------------------------- representatives ---


@router.post(
    "/representatives",
    response_model=RepresentativeOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(MANAGE_TENANT))],
)
async def create_representative(
    payload: RepresentativeRequest, session: SessionDep, tenant: TenantDep
) -> RepresentativeOut:
    representative = Representative(tenant_id=tenant.id, **payload.model_dump())
    session.add(representative)
    await session.commit()
    return RepresentativeOut.model_validate(representative)


# ------------------------------------------------------------------- audit ---


@router.get(
    "/audit",
    response_model=Page[dict],
    dependencies=[Depends(require_permission(perm(Resource.AUDIT, "read")))],
)
async def audit_trail(
    session: SessionDep,
    tenant: TenantDep,
    page: PageDep,
    entity_type: Annotated[str | None, Query(max_length=64)] = None,
    entity_id: Annotated[uuid.UUID | None, Query()] = None,
    actor_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[dict]:
    """Read the audit trail. The record of who changed what, and when."""
    rows, total = await audit_service.history(
        session,
        tenant.id,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        page=page,
    )
    items = [
        {
            "id": str(row.id),
            "action": str(row.action),
            "entity_type": row.entity_type,
            "entity_id": str(row.entity_id) if row.entity_id else None,
            "entity_label": row.entity_label,
            "actor_label": row.actor_label,
            "actor_role": row.actor_role,
            "summary": row.summary,
            "before": row.before,
            "after": row.after,
            "succeeded": row.succeeded,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
    result = PageResult.build(items, total, page)
    return Page[dict].model_validate(result.model_dump())


@router.delete(
    "/audit/purge",
    response_model=Message,
    dependencies=[Depends(require_superadmin())],
)
async def purge_audit(
    session: SessionDep,
    tenant: TenantDep,
    older_than_days: Annotated[int, Query(ge=365, le=3650)] = 1825,
) -> Message:
    """Purge audit entries older than a retention period (minimum one year)."""
    from datetime import timedelta

    from sqlalchemy import delete

    from civicos.core.clock import utcnow

    cutoff = utcnow() - timedelta(days=older_than_days)
    result = await session.execute(
        delete(AuditLog).where(AuditLog.tenant_id == tenant.id, AuditLog.created_at < cutoff)
    )
    await session.commit()
    return Message(message=f"{result.rowcount or 0} audit entries purged.")
