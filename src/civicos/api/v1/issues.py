"""Issue endpoints - the busiest surface in the platform."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy import inspect as sa_inspect

from civicos.api.deps import (
    ActorDep,
    CurrentUserDep,
    OptionalActorDep,
    PageDep,
    SessionDep,
    TenantDep,
    require_permission,
)
from civicos.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from civicos.core.geo import Point
from civicos.core.pagination import Page as PageResult
from civicos.core.permissions import Resource, perm
from civicos.domain.enums import (
    IssueStatus,
    Priority,
    ReportChannel,
    Visibility,
)
from civicos.domain.issues import Issue
from civicos.repositories.issues import IssueFilters, IssueRepository
from civicos.schemas.common import AttachmentOut, Message, Page, TimelineEntry
from civicos.schemas.issues import (
    DuplicateCandidateOut,
    IssueAssignRequest,
    IssueCommentOut,
    IssueCommentRequest,
    IssueCreatedOut,
    IssueCreateRequest,
    IssueDetail,
    IssueEscalateRequest,
    IssueMergeRequest,
    IssueRatingRequest,
    IssueSummary,
    IssueTransitionRequest,
    IssueUpdateRequest,
    NearbyIssueOut,
)
from civicos.services import audit_service, issue_service
from civicos.services.attachment_service import attach_to_issue

router = APIRouter(prefix="/issues", tags=["Issues"])

READ = perm(Resource.ISSUE, "read")
UPDATE = perm(Resource.ISSUE, "update")


@router.post("", response_model=IssueCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_issue(
    payload: IssueCreateRequest,
    session: SessionDep,
    tenant: TenantDep,
    actor: OptionalActorDep,
) -> IssueCreatedOut:
    """Report a civic issue.

    Open to anonymous submissions when the deployment allows it - the barrier to
    reporting a broken streetlight should be as close to zero as possible.
    """
    from civicos.core.config import get_settings

    settings = get_settings()
    if not actor.is_authenticated and not settings.security.allow_anonymous_reports:
        raise PermissionDeniedError("Sign in to report an issue.", code="authentication_required")

    # Only staff may hand-set a priority; a resident's urgency is an input to
    # triage, not a decision.
    priority = payload.priority if actor.has_permission(UPDATE) else None

    draft = issue_service.IssueDraft(
        title=payload.title,
        description=payload.description,
        category_slug=payload.category_slug,
        priority=priority,
        channel=payload.channel,
        latitude=payload.location.latitude if payload.location else None,
        longitude=payload.location.longitude if payload.location else None,
        location_accuracy_m=payload.location.accuracy_meters if payload.location else None,
        address=payload.address,
        landmark=payload.landmark,
        language=payload.language,
        reporter_id=actor.id if actor.kind == "user" else None,
        reporter_name=payload.reporter_name,
        reporter_phone=payload.reporter_phone,
        reporter_email=payload.reporter_email,
        is_anonymous=payload.is_anonymous,
        contact_consent=payload.contact_consent,
        tags=payload.tags,
        extra=payload.extra,
    )
    outcome = await issue_service.report_issue(session, tenant, draft)
    await session.commit()
    # Reload with its collections eagerly loaded: the response includes the
    # timeline, and a lazy load after commit would fail on an async session.
    stored = await _load(session, tenant.id, outcome.issue.id)

    return IssueCreatedOut(
        issue=_to_detail(stored),
        created=outcome.created,
        merged_into=outcome.merged_into.reference if outcome.merged_into else None,
        duplicate_candidates=[
            DuplicateCandidateOut(**candidate) for candidate in outcome.duplicate_candidates
        ],
        ai_applied=outcome.ai_applied,
        routing_reason=outcome.routing_reason,
        warnings=outcome.warnings,
    )


@router.get("", response_model=Page[IssueSummary], dependencies=[Depends(require_permission(READ))])
async def list_issues(
    session: SessionDep,
    tenant: TenantDep,
    page: PageDep,
    status_filter: Annotated[list[IssueStatus] | None, Query(alias="status")] = None,
    priority: Annotated[list[Priority] | None, Query()] = None,
    channel: Annotated[list[ReportChannel] | None, Query()] = None,
    category_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    department_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    admin_unit_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    assigned_to_id: Annotated[uuid.UUID | None, Query()] = None,
    unassigned: Annotated[bool, Query()] = False,
    open_only: Annotated[bool, Query()] = False,
    breached_only: Annotated[bool, Query()] = False,
    flagged_only: Annotated[bool, Query()] = False,
    search: Annotated[str | None, Query(max_length=200)] = None,
    tags: Annotated[list[str] | None, Query()] = None,
    sort_by: Annotated[str | None, Query()] = None,
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
) -> Page[IssueSummary]:
    """Filtered, paginated issue list - the operations queue."""
    filters = IssueFilters(
        status=status_filter or [],
        priority=priority or [],
        channel=channel or [],
        category_ids=category_id or [],
        department_ids=department_id or [],
        admin_unit_ids=admin_unit_id or [],
        assigned_to_id=assigned_to_id,
        unassigned=unassigned,
        open_only=open_only,
        breached_only=breached_only,
        flagged_only=flagged_only,
        search=search,
        tags=tags or [],
    )
    repository = IssueRepository(session, tenant.id)
    items, total = await repository.search(
        filters, page, sort_by=sort_by, descending=order == "desc"
    )
    result = PageResult.build([IssueSummary.model_validate(issue) for issue in items], total, page)
    return Page[IssueSummary].model_validate(result.model_dump())


@router.get("/mine", response_model=Page[IssueSummary])
async def my_issues(
    session: SessionDep, tenant: TenantDep, page: PageDep, user: CurrentUserDep
) -> Page[IssueSummary]:
    """Reports filed by the signed-in resident."""
    repository = IssueRepository(session, tenant.id)
    items, total = await repository.search(
        IssueFilters(reporter_id=user.id), page, sort_by="created_at"
    )
    result = PageResult.build([IssueSummary.model_validate(issue) for issue in items], total, page)
    return Page[IssueSummary].model_validate(result.model_dump())


@router.get("/assigned", response_model=Page[IssueSummary])
async def assigned_to_me(
    session: SessionDep, tenant: TenantDep, page: PageDep, user: CurrentUserDep
) -> Page[IssueSummary]:
    """The signed-in officer's own work queue."""
    repository = IssueRepository(session, tenant.id)
    items, total = await repository.search(
        IssueFilters(assigned_to_id=user.id, open_only=True),
        page,
        sort_by="resolution_due_at",
        descending=False,
    )
    result = PageResult.build([IssueSummary.model_validate(issue) for issue in items], total, page)
    return Page[IssueSummary].model_validate(result.model_dump())


@router.get("/nearby", response_model=list[NearbyIssueOut])
async def nearby(
    session: SessionDep,
    tenant: TenantDep,
    latitude: Annotated[float, Query(ge=-90, le=90)],
    longitude: Annotated[float, Query(ge=-180, le=180)],
    radius_meters: Annotated[int, Query(ge=50, le=5000)] = 500,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[NearbyIssueOut]:
    """What has already been reported near a point.

    Shown before a resident submits, which prevents duplicates at the source -
    far cheaper than merging them afterwards.
    """
    rows = await issue_service.nearby_issues(
        session, tenant.id, Point(latitude, longitude), radius_meters, limit
    )
    return [
        NearbyIssueOut(
            reference=issue.reference,
            title=issue.title,
            status=issue.status,
            category=issue.category.name if issue.category else None,
            latitude=issue.latitude,
            longitude=issue.longitude,
            admin_unit=issue.admin_unit.name if issue.admin_unit else None,
            confirmations=issue.confirmations,
            created_at=issue.created_at,
            resolved_at=issue.resolved_at,
            distance_meters=round(distance, 1),
        )
        for issue, distance in rows
        if issue.visibility is Visibility.PUBLIC
    ]


@router.get("/reference/{reference}", response_model=IssueDetail)
async def get_by_reference(
    reference: str, session: SessionDep, tenant: TenantDep, actor: OptionalActorDep
) -> IssueDetail:
    """Track a report by the code the resident was given. No account needed."""
    repository = IssueRepository(session, tenant.id)
    issue = await repository.get_by_reference(reference)
    if issue is None:
        raise NotFoundError("No report with that reference.", code="issue_not_found")
    full = await repository.get_with_timeline(issue.id)
    return _to_detail(full or issue, include_internal=actor.has_permission(READ))


@router.get("/{issue_id}", response_model=IssueDetail)
async def get_issue(
    issue_id: uuid.UUID, session: SessionDep, tenant: TenantDep, actor: OptionalActorDep
) -> IssueDetail:
    issue = await _load(session, tenant.id, issue_id)
    is_reporter = actor.id is not None and issue.reporter_id == actor.id
    if not (actor.has_permission(READ) or is_reporter or issue.visibility is Visibility.PUBLIC):
        raise PermissionDeniedError("You cannot view this report.")
    issue.view_count += 1
    await session.commit()
    return _to_detail(issue, include_internal=actor.has_permission(READ) or is_reporter)


@router.patch(
    "/{issue_id}", response_model=IssueDetail, dependencies=[Depends(require_permission(UPDATE))]
)
async def update_issue(
    issue_id: uuid.UUID,
    payload: IssueUpdateRequest,
    session: SessionDep,
    tenant: TenantDep,
) -> IssueDetail:
    issue = await _load(session, tenant.id, issue_id)
    before = audit_service.snapshot(
        issue, ("title", "category_id", "department_id", "priority", "severity", "visibility")
    )
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(issue, field, value)
    await audit_service.record_change(
        session,
        entity_type="issue",
        entity_id=issue.id,
        entity_label=issue.reference,
        before=before,
        after=audit_service.snapshot(
            issue,
            ("title", "category_id", "department_id", "priority", "severity", "visibility"),
        ),
    )
    await session.commit()
    return _to_detail(await _load(session, tenant.id, issue_id))


@router.post(
    "/{issue_id}/transition",
    response_model=IssueDetail,
    dependencies=[Depends(require_permission(perm(Resource.ISSUE, "transition")))],
)
async def transition_issue(
    issue_id: uuid.UUID,
    payload: IssueTransitionRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> IssueDetail:
    """Move a report through the workflow. Invalid transitions are rejected."""
    issue = await _load(session, tenant.id, issue_id)
    await issue_service.transition(
        session,
        tenant,
        issue,
        payload.status,
        note=payload.note,
        actor=user,
        resolution_note=payload.resolution_note,
        rejection_reason=payload.rejection_reason,
    )
    await session.commit()
    # Re-read so the response reflects committed state rather than an
    # in-memory object whose relationships the commit may have expired.
    return _to_detail(await _load(session, tenant.id, issue_id))


@router.post(
    "/{issue_id}/assign",
    response_model=IssueDetail,
    dependencies=[Depends(require_permission(perm(Resource.ISSUE, "assign")))],
)
async def assign_issue(
    issue_id: uuid.UUID,
    payload: IssueAssignRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> IssueDetail:
    issue = await _load(session, tenant.id, issue_id)
    await issue_service.assign(
        session, tenant, issue, payload.assignee_id, note=payload.note, actor=user
    )
    await session.commit()
    return _to_detail(await _load(session, tenant.id, issue_id))


@router.post(
    "/{issue_id}/merge",
    response_model=Message,
    dependencies=[Depends(require_permission(perm(Resource.ISSUE, "merge")))],
)
async def merge_issue(
    issue_id: uuid.UUID,
    payload: IssueMergeRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> Message:
    issue = await _load(session, tenant.id, issue_id)
    parent = await _load(session, tenant.id, payload.parent_issue_id)
    await issue_service.merge_duplicate(
        session, issue, parent, reason=payload.reason or "", actor=user
    )
    await session.commit()
    return Message(
        message=f"{issue.reference} merged into {parent.reference}.",
        detail=f"{parent.reference} now has {parent.confirmations} corroborating report(s).",
    )


@router.post("/{issue_id}/comments", response_model=IssueCommentOut, status_code=201)
async def add_comment(
    issue_id: uuid.UUID,
    payload: IssueCommentRequest,
    session: SessionDep,
    tenant: TenantDep,
    actor: ActorDep,
    user: CurrentUserDep,
) -> IssueCommentOut:
    issue = await _load(session, tenant.id, issue_id)
    is_reporter = issue.reporter_id == user.id
    if not (actor.has_permission(perm(Resource.ISSUE, "comment")) or is_reporter):
        raise PermissionDeniedError("You cannot comment on this report.")

    # Only staff may post internal notes or speak officially.
    visibility = payload.visibility if actor.has_permission(READ) else Visibility.PUBLIC
    is_official = payload.is_official and actor.has_permission(UPDATE)

    comment = await issue_service.add_comment(
        session, issue, payload.body, author=user, visibility=visibility, is_official=is_official
    )
    await session.commit()
    return IssueCommentOut.model_validate(comment)


@router.post("/{issue_id}/confirm", response_model=Message)
async def confirm_issue(
    issue_id: uuid.UUID, session: SessionDep, tenant: TenantDep, user: CurrentUserDep
) -> Message:
    """ "I see this too." Corroboration drives prioritisation."""
    issue = await _load(session, tenant.id, issue_id)
    await issue_service.follow(session, issue, user.id, confirmed=True)
    await session.commit()
    return Message(
        message="Thank you - your confirmation has been recorded.",
        detail=f"{issue.confirmations} resident(s) have now confirmed this report.",
    )


@router.post("/{issue_id}/rate", response_model=Message)
async def rate_issue(
    issue_id: uuid.UUID,
    payload: IssueRatingRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> Message:
    issue = await _load(session, tenant.id, issue_id)
    await issue_service.rate_resolution(
        session, issue, payload.rating, comment=payload.comment, rater_id=user.id
    )
    await session.commit()
    return Message(message="Thank you for your feedback.")


@router.post(
    "/{issue_id}/escalate",
    response_model=Message,
    dependencies=[Depends(require_permission(perm(Resource.ISSUE, "escalate")))],
)
async def escalate_issue(
    issue_id: uuid.UUID,
    payload: IssueEscalateRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> Message:
    issue = await _load(session, tenant.id, issue_id)
    await issue_service.escalate(session, tenant, issue, reason=payload.reason, actor=user)
    await session.commit()
    return Message(message=f"{issue.reference} escalated to level {issue.escalation_level}.")


@router.post(
    "/{issue_id}/attachments",
    response_model=list[AttachmentOut],
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachments(
    issue_id: uuid.UUID,
    session: SessionDep,
    tenant: TenantDep,
    actor: OptionalActorDep,
    files: Annotated[list[UploadFile], File(description="Photos or documents.")],
    stage: Annotated[
        str, Query(pattern="^(report|before|progress|after|verification)$")
    ] = "report",
    analyse: Annotated[bool, Query(description="Run AI analysis on the images.")] = False,
) -> list[AttachmentOut]:
    """Attach evidence to a report, extracting EXIF location and capture time."""
    issue = await _load(session, tenant.id, issue_id)
    is_reporter = actor.id is not None and issue.reporter_id == actor.id
    if not (actor.has_permission(UPDATE) or is_reporter):
        raise PermissionDeniedError("You cannot add attachments to this report.")
    if len(files) > 10:
        raise ValidationError("At most 10 files per request.", code="too_many_files")

    attachments = await attach_to_issue(
        session, tenant, issue, files, stage=stage, analyse=analyse, uploader_id=actor.id
    )
    await session.commit()
    return [AttachmentOut.model_validate(item) for item in attachments]


@router.get("/{issue_id}/timeline", response_model=list[TimelineEntry])
async def timeline(
    issue_id: uuid.UUID, session: SessionDep, tenant: TenantDep, actor: OptionalActorDep
) -> list[TimelineEntry]:
    repository = IssueRepository(session, tenant.id)
    await repository.get_or_404(issue_id)
    events = await repository.timeline(issue_id)
    include_internal = actor.has_permission(READ)
    return [
        TimelineEntry.model_validate(event)
        for event in events
        if include_internal or event.is_public
    ]


# ---------------------------------------------------------------- helpers ----


async def _load(session: Any, tenant_id: uuid.UUID, issue_id: uuid.UUID) -> Issue:
    repository = IssueRepository(session, tenant_id)
    issue = await repository.get_with_timeline(issue_id)
    if issue is None:
        raise NotFoundError("Report not found.", code="issue_not_found")
    return issue


def _to_detail(issue: Issue, *, include_internal: bool = True) -> IssueDetail:
    """Serialise an issue, hiding internal notes from the public view."""
    # Only read collections that were eagerly loaded - touching an unloaded
    # relationship on an async session raises rather than lazily querying.
    unloaded = sa_inspect(issue).unloaded
    events = list(issue.events) if "events" not in unloaded else []
    comments = list(issue.comments) if "comments" not in unloaded else []
    attachments = list(issue.attachments) if "attachments" not in unloaded else []

    detail = IssueDetail.model_validate(
        {
            **{column.key: getattr(issue, column.key) for column in issue.__table__.columns},
            "category": issue.category,
            "department": issue.department,
            "assignee": issue.assignee,
            "attachments": [],
            "timeline": [],
            "comments": [],
        }
    )
    detail.sla = _sla_payload(issue)
    detail.ai = _ai_payload(issue)

    detail.timeline = [
        TimelineEntry.model_validate(event)
        for event in events
        if include_internal or event.is_public
    ]
    detail.comments = [
        IssueCommentOut.model_validate(comment)
        for comment in comments
        if (include_internal or comment.visibility is Visibility.PUBLIC) and not comment.is_deleted
    ]
    detail.attachments = [AttachmentOut.model_validate(item) for item in attachments]

    if not include_internal:
        detail.reporter_name = None
        detail.ai = None
    return detail


def _sla_payload(issue: Issue) -> dict[str, Any]:
    from civicos.schemas.issues import SLAOut

    return SLAOut(
        response_due_at=issue.response_due_at,
        resolution_due_at=issue.resolution_due_at,
        first_response_at=issue.first_response_at,
        resolved_at=issue.resolved_at,
        response_state=issue.sla_response_state,
        resolution_state=issue.sla_resolution_state,
        escalation_level=issue.escalation_level,
    )


def _ai_payload(issue: Issue) -> Any:
    from civicos.schemas.issues import AITriageOut

    if not issue.ai_triaged_at:
        return None
    return AITriageOut(
        category_slug=issue.ai_category_slug,
        confidence=issue.ai_confidence,
        priority=issue.ai_priority,
        severity=issue.ai_severity,
        summary=issue.ai_summary,
        model=issue.ai_model,
        triaged_at=issue.ai_triaged_at,
        analysis=issue.ai_analysis,
    )
