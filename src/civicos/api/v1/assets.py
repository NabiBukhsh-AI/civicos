"""Asset registry and citizen-service (permit / licence) endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, or_, select

from civicos.api.deps import (
    CurrentUserDep,
    OptionalActorDep,
    PageDep,
    SessionDep,
    TenantDep,
    require_permission,
)
from civicos.core.clock import utcnow
from civicos.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from civicos.core.geo import Point, bounding_box, encode_geohash, haversine_meters
from civicos.core.pagination import Page as PageResult
from civicos.core.permissions import Resource, perm
from civicos.core.security import generate_reference
from civicos.domain.assets import Asset, AssetInspection
from civicos.domain.enums import (
    SERVICE_TRANSITIONS,
    AssetCondition,
    AssetType,
    ServiceApplicationStatus,
)
from civicos.domain.services import ApplicationEvent, ServiceApplication, ServiceType
from civicos.schemas.common import Message, Page
from civicos.schemas.operations import (
    AssetCreateRequest,
    AssetInspectionOut,
    AssetInspectionRequest,
    AssetOut,
    ServiceApplicationOut,
    ServiceApplicationRequest,
    ServiceDecisionRequest,
    ServiceTypeOut,
)

router = APIRouter(prefix="/assets", tags=["Assets"])
services_router = APIRouter(prefix="/services", tags=["Citizen Services"])

ASSET_READ = perm(Resource.ASSET, "read")


@router.post(
    "",
    response_model=AssetOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(Resource.ASSET, "create")))],
)
async def create_asset(
    payload: AssetCreateRequest, session: SessionDep, tenant: TenantDep
) -> AssetOut:
    """Register a municipal asset.

    The generated ``qr_payload`` is meant to be printed on a sticker: scanning a
    broken streetlight then files a pre-located, pre-categorised report.
    """
    asset = Asset(tenant_id=tenant.id, **payload.model_dump())
    if payload.latitude is not None and payload.longitude is not None:
        asset.geohash = encode_geohash(payload.latitude, payload.longitude)
    asset.qr_payload = f"civicos:{tenant.slug}:asset:{payload.code}"
    if payload.inspection_interval_days:
        from datetime import timedelta  # noqa: PLC0415

        asset.next_inspection_due = utcnow() + timedelta(days=payload.inspection_interval_days)

    session.add(asset)
    await session.commit()
    return AssetOut.model_validate(asset)


@router.get("", response_model=Page[AssetOut], dependencies=[Depends(require_permission(ASSET_READ))])
async def list_assets(
    session: SessionDep,
    tenant: TenantDep,
    page: PageDep,
    asset_type: Annotated[AssetType | None, Query()] = None,
    condition: Annotated[AssetCondition | None, Query()] = None,
    admin_unit_id: Annotated[uuid.UUID | None, Query()] = None,
    department_id: Annotated[uuid.UUID | None, Query()] = None,
    needs_inspection: Annotated[bool, Query()] = False,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> Page[AssetOut]:
    statement = select(Asset).where(Asset.tenant_id == tenant.id, Asset.deleted_at.is_(None))
    if asset_type:
        statement = statement.where(Asset.asset_type == asset_type)
    if condition:
        statement = statement.where(Asset.condition == condition)
    if admin_unit_id:
        statement = statement.where(Asset.admin_unit_id == admin_unit_id)
    if department_id:
        statement = statement.where(Asset.department_id == department_id)
    if needs_inspection:
        statement = statement.where(
            Asset.next_inspection_due.is_not(None), Asset.next_inspection_due <= utcnow()
        )
    if search:
        term = f"%{search.lower()}%"
        statement = statement.where(
            or_(func.lower(Asset.name).like(term), func.lower(Asset.code).like(term))
        )

    total = int(
        await session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    )
    rows = (
        await session.scalars(
            statement.order_by(Asset.code).offset(page.offset).limit(page.limit)
        )
    ).unique().all()
    result = PageResult.build([AssetOut.model_validate(row) for row in rows], total, page)
    return Page[AssetOut].model_validate(result.model_dump())


@router.get("/nearby", response_model=list[dict], dependencies=[Depends(require_permission(ASSET_READ))])
async def nearby_assets(
    session: SessionDep,
    tenant: TenantDep,
    latitude: Annotated[float, Query(ge=-90, le=90)],
    longitude: Annotated[float, Query(ge=-180, le=180)],
    radius_meters: Annotated[int, Query(ge=10, le=5000)] = 200,
    asset_type: Annotated[AssetType | None, Query()] = None,
) -> list[dict[str, Any]]:
    """Find assets near a point - used to attach a report to the right pole or drain."""
    point = Point(latitude, longitude)
    box = bounding_box(point, radius_meters)
    statement = select(Asset).where(
        Asset.tenant_id == tenant.id,
        Asset.deleted_at.is_(None),
        Asset.latitude.between(box.min_latitude, box.max_latitude),
        Asset.longitude.between(box.min_longitude, box.max_longitude),
    )
    if asset_type:
        statement = statement.where(Asset.asset_type == asset_type)

    rows = (await session.scalars(statement.limit(200))).unique().all()
    scored = [
        (asset, haversine_meters(point, Point(asset.latitude, asset.longitude)))
        for asset in rows
        if asset.latitude is not None and asset.longitude is not None
    ]
    scored = sorted(
        (pair for pair in scored if pair[1] <= radius_meters), key=lambda pair: pair[1]
    )
    return [
        {
            "id": str(asset.id),
            "code": asset.code,
            "name": asset.name,
            "asset_type": str(asset.asset_type),
            "condition": str(asset.condition),
            "distance_meters": round(distance, 1),
        }
        for asset, distance in scored[:50]
    ]


@router.get("/code/{code}", response_model=AssetOut)
async def get_asset_by_code(
    code: str, session: SessionDep, tenant: TenantDep
) -> AssetOut:
    """Look up an asset by the code stencilled on it (or encoded in its QR)."""
    asset = await session.scalar(
        select(Asset).where(
            Asset.tenant_id == tenant.id, Asset.code == code, Asset.deleted_at.is_(None)
        )
    )
    if asset is None:
        raise NotFoundError("No asset with that code.", code="asset_not_found")
    return AssetOut.model_validate(asset)


@router.post(
    "/{asset_id}/inspections",
    response_model=AssetInspectionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(Resource.ASSET, "inspect")))],
)
async def record_inspection(
    asset_id: uuid.UUID,
    payload: AssetInspectionRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> AssetInspectionOut:
    """Record a condition assessment and reschedule the next inspection."""
    from datetime import timedelta  # noqa: PLC0415

    asset = await session.get(Asset, asset_id)
    if asset is None or asset.tenant_id != tenant.id:
        raise NotFoundError("Asset not found.", code="asset_not_found")

    inspection = AssetInspection(
        tenant_id=tenant.id,
        asset_id=asset.id,
        inspector_id=user.id,
        inspected_at=payload.inspected_at or utcnow(),
        condition=payload.condition,
        severity=payload.severity,
        findings=payload.findings,
        recommended_action=payload.recommended_action,
        estimated_repair_cost=payload.estimated_repair_cost,
    )
    session.add(inspection)

    asset.condition = payload.condition
    asset.last_inspected_at = inspection.inspected_at
    asset.is_operational = payload.condition not in {
        AssetCondition.CRITICAL,
        AssetCondition.DECOMMISSIONED,
    }
    if asset.inspection_interval_days:
        asset.next_inspection_due = utcnow() + timedelta(days=asset.inspection_interval_days)

    await session.commit()
    return AssetInspectionOut.model_validate(inspection)


# ------------------------------------------------------- citizen services ----


@services_router.get("/types", response_model=list[ServiceTypeOut])
async def list_service_types(
    session: SessionDep, tenant: TenantDep
) -> list[ServiceTypeOut]:
    rows = (
        await session.scalars(
            select(ServiceType)
            .where(ServiceType.tenant_id == tenant.id, ServiceType.is_active.is_(True))
            .order_by(ServiceType.display_order, ServiceType.name)
        )
    ).all()
    return [ServiceTypeOut.model_validate(row) for row in rows]


@services_router.post(
    "/applications",
    response_model=ServiceApplicationOut,
    status_code=status.HTTP_201_CREATED,
)
async def apply(
    payload: ServiceApplicationRequest,
    session: SessionDep,
    tenant: TenantDep,
    actor: OptionalActorDep,
) -> ServiceApplicationOut:
    """Apply for a permit, licence, NOC or certificate."""
    from datetime import timedelta  # noqa: PLC0415

    service_type = await session.get(ServiceType, payload.service_type_id)
    if service_type is None or service_type.tenant_id != tenant.id:
        raise NotFoundError("Service not found.", code="service_type_not_found")
    if not service_type.is_active:
        raise ValidationError("That service is not currently accepting applications.")

    missing = [
        field.get("code")
        for field in service_type.form_schema or []
        if field.get("required") and field.get("code") not in payload.form_data
    ]
    if missing and payload.submit:
        raise ValidationError(
            "Required fields are missing.",
            code="incomplete_application",
            details={"missing": missing},
        )

    application = ServiceApplication(
        tenant_id=tenant.id,
        reference=generate_reference(f"{(tenant.slug[:2] or 'sv').upper()}S"),
        service_type_id=service_type.id,
        status=(
            ServiceApplicationStatus.SUBMITTED
            if payload.submit
            else ServiceApplicationStatus.DRAFT
        ),
        applicant_id=actor.id if actor.kind == "user" else None,
        applicant_name=payload.applicant_name,
        applicant_phone=payload.applicant_phone,
        applicant_email=payload.applicant_email,
        form_data=payload.form_data,
        premises_address=payload.premises_address,
        latitude=payload.latitude,
        longitude=payload.longitude,
        fee_amount=service_type.fee_amount,
        submitted_at=utcnow() if payload.submit else None,
        due_at=(
            utcnow() + timedelta(days=service_type.processing_days) if payload.submit else None
        ),
    )
    session.add(application)
    await session.flush()

    session.add(
        ApplicationEvent(
            tenant_id=tenant.id,
            application_id=application.id,
            actor_id=application.applicant_id,
            actor_label=payload.applicant_name,
            to_status=str(application.status),
            note="Application submitted." if payload.submit else "Draft saved.",
        )
    )
    await session.commit()
    return ServiceApplicationOut.model_validate(application)


@services_router.get("/applications", response_model=Page[ServiceApplicationOut])
async def list_applications(
    session: SessionDep,
    tenant: TenantDep,
    page: PageDep,
    actor: OptionalActorDep,
    status_filter: Annotated[ServiceApplicationStatus | None, Query(alias="status")] = None,
    mine: Annotated[bool, Query(description="Only my own applications.")] = False,
) -> Page[ServiceApplicationOut]:
    can_read_all = actor.has_permission(perm(Resource.SERVICE_REQUEST, "read"))
    if not can_read_all and not mine:
        mine = True  # residents may only see their own

    statement = select(ServiceApplication).where(
        ServiceApplication.tenant_id == tenant.id,
        ServiceApplication.deleted_at.is_(None),
    )
    if mine:
        if not actor.is_authenticated:
            raise PermissionDeniedError("Sign in to view your applications.")
        statement = statement.where(ServiceApplication.applicant_id == actor.id)
    if status_filter:
        statement = statement.where(ServiceApplication.status == status_filter)

    total = int(
        await session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    )
    rows = (
        await session.scalars(
            statement.order_by(ServiceApplication.created_at.desc())
            .offset(page.offset)
            .limit(page.limit)
        )
    ).unique().all()
    result = PageResult.build(
        [ServiceApplicationOut.model_validate(row) for row in rows], total, page
    )
    return Page[ServiceApplicationOut].model_validate(result.model_dump())


@services_router.post(
    "/applications/{application_id}/decide",
    response_model=ServiceApplicationOut,
    dependencies=[Depends(require_permission(perm(Resource.SERVICE_REQUEST, "decide")))],
)
async def decide_application(
    application_id: uuid.UUID,
    payload: ServiceDecisionRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> ServiceApplicationOut:
    """Advance an application through its approval workflow."""
    application = await session.get(ServiceApplication, application_id)
    if application is None or application.tenant_id != tenant.id:
        raise NotFoundError("Application not found.", code="application_not_found")

    current = application.status
    allowed = SERVICE_TRANSITIONS.get(current, frozenset())
    if payload.status not in allowed:
        from civicos.core.errors import WorkflowError  # noqa: PLC0415

        raise WorkflowError(
            f"Cannot move an application from '{current.value}' to '{payload.status.value}'.",
            details={"allowed": sorted(status.value for status in allowed)},
        )

    application.status = payload.status
    application.decision_note = payload.note
    application.info_request = payload.info_request
    if payload.status in {
        ServiceApplicationStatus.APPROVED,
        ServiceApplicationStatus.REJECTED,
    }:
        application.decided_at = utcnow()
    if payload.status is ServiceApplicationStatus.ISSUED:
        application.certificate_number = payload.certificate_number
        application.issued_on = utcnow().date()
        application.valid_until = payload.valid_until

    session.add(
        ApplicationEvent(
            tenant_id=tenant.id,
            application_id=application.id,
            actor_id=user.id,
            actor_label=user.display(),
            from_status=current.value,
            to_status=payload.status.value,
            note=payload.note,
        )
    )

    template = {
        ServiceApplicationStatus.APPROVED: "service.approved",
        ServiceApplicationStatus.REJECTED: "service.rejected",
        ServiceApplicationStatus.INFO_REQUIRED: "service.info_required",
    }.get(payload.status)
    if template:
        from civicos.services.notification_service import (  # noqa: PLC0415
            notify,
            recipient_from_contact,
        )

        recipient = recipient_from_contact(
            application.applicant_phone, application.applicant_email
        )
        if recipient is not None:
            await notify(
                session,
                tenant.id,
                [recipient],
                template_key=template,
                params={
                    "reference": application.reference,
                    "reason": payload.note or "",
                },
                entity_type="service_application",
                entity_id=application.id,
            )

    await session.commit()
    return ServiceApplicationOut.model_validate(application)
