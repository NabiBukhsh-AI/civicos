"""Issue lifecycle: intake, triage, assignment, transitions, resolution.

This is the core workflow of the platform. The intake path is deliberately
ordered so that a report is **persisted before anything clever happens to it**:
moderation, AI triage, duplicate detection and routing all run against a saved
record. A resident's complaint is never lost because a model timed out.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.ai.language import moderate_text
from civicos.ai.registry import is_ai_enabled
from civicos.ai.triage import is_confident, triage_report
from civicos.ai.usage import UsageContext, run_embedding
from civicos.core import context
from civicos.core.clock import utcnow
from civicos.core.errors import PermissionDeniedError, ValidationError, WorkflowError
from civicos.core.geo import Point, encode_geohash, is_valid_coordinate
from civicos.core.i18n import translate
from civicos.core.security import generate_reference
from civicos.core.telemetry import ISSUE_TRANSITIONS, ISSUES_CREATED
from civicos.core.text import guess_language, normalise_whitespace, summarise_for_title, truncate
from civicos.domain.enums import (
    ISSUE_TRANSITIONS as ALLOWED_TRANSITIONS,
)
from civicos.domain.enums import (
    AuditAction,
    IssueEventType,
    IssueStatus,
    Priority,
    ReportChannel,
    Severity,
    SLAState,
    Visibility,
)
from civicos.domain.identity import User
from civicos.domain.issues import Issue, IssueComment, IssueEvent, IssueFollower
from civicos.domain.tenancy import Department, IssueCategory, Municipality
from civicos.repositories.issues import IssueRepository
from civicos.services import audit_service, duplicate_service, routing_service, sla_service
from civicos.services.notification_service import Recipient, notify, recipient_from_contact

logger = structlog.get_logger(__name__)

#: Statuses that mean a human has responded to the reporter.
_RESPONSE_STATUSES = {
    IssueStatus.ACKNOWLEDGED,
    IssueStatus.ASSIGNED,
    IssueStatus.IN_PROGRESS,
    IssueStatus.RESOLVED,
}

_TRACKED_FIELDS = (
    "status",
    "priority",
    "severity",
    "category_id",
    "department_id",
    "assigned_to_id",
    "visibility",
)


@dataclass(slots=True)
class IssueDraft:
    """Everything a caller can supply when reporting an issue."""

    title: str | None = None
    description: str = ""
    category_slug: str | None = None
    priority: Priority | None = None
    channel: ReportChannel = ReportChannel.WEB
    latitude: float | None = None
    longitude: float | None = None
    location_accuracy_m: float | None = None
    address: str | None = None
    landmark: str | None = None
    language: str | None = None
    reporter_id: uuid.UUID | None = None
    reporter_name: str | None = None
    reporter_phone: str | None = None
    reporter_email: str | None = None
    is_anonymous: bool = False
    contact_consent: bool = True
    tags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class IntakeOutcome:
    """The result of an intake attempt, including what the AI proposed."""

    issue: Issue
    created: bool
    merged_into: Issue | None = None
    duplicate_candidates: list[dict[str, Any]] = field(default_factory=list)
    ai_applied: bool = False
    routing_reason: str = ""
    warnings: list[str] = field(default_factory=list)


async def report_issue(
    session: AsyncSession,
    tenant: Municipality,
    draft: IssueDraft,
    *,
    run_ai: bool = True,
    auto_assign: bool = True,
    usage: UsageContext | None = None,
) -> IntakeOutcome:
    """Accept a new civic report and take it as far through triage as it can go."""
    description = normalise_whitespace(draft.description)
    if len(description) < 10:
        raise ValidationError(
            "Please describe the problem in at least a few words.",
            code="description_too_short",
        )

    usage = usage or UsageContext.from_request(session, tenant_id=tenant.id)
    warnings: list[str] = []
    language = draft.language or guess_language(description, tenant.default_language)

    # 1. Hygiene pass. Always runs; cheap, deterministic and never blocks intake.
    moderation = await moderate_text(description, usage=usage, use_model=run_ai and is_ai_enabled())
    stored_description = moderation.redacted_text or description
    if moderation.contains_personal_data:
        warnings.append("Personal details were removed from the public description.")

    title = normalise_whitespace(draft.title or "") or summarise_for_title(description)
    latitude, longitude = _validated_location(draft, warnings)

    fingerprint = duplicate_service.fingerprint_for(title, description, latitude, longitude)

    # 2. Exact-resubmission check before we write anything.
    repository = IssueRepository(session, tenant.id)
    existing = await repository.find_by_fingerprint(fingerprint, within_hours=24)
    if existing is not None:
        logger.info("issue_intake_deduplicated", reference=existing.reference)
        await _register_confirmation(session, existing, draft.reporter_id)
        return IntakeOutcome(
            issue=existing,
            created=False,
            merged_into=existing,
            warnings=["This matches a report you already filed."],
        )

    # 3. Persist immediately. Everything after this point is enrichment.
    issue = Issue(
        tenant_id=tenant.id,
        reference=await _unique_reference(repository, tenant),
        title=truncate(title, 255),
        description=stored_description,
        description_original=description if stored_description != description else None,
        language=language,
        channel=draft.channel,
        priority=draft.priority or Priority.NORMAL,
        latitude=latitude,
        longitude=longitude,
        geohash=encode_geohash(latitude, longitude) if latitude is not None else None,
        location_accuracy_m=draft.location_accuracy_m,
        address=draft.address,
        landmark=draft.landmark,
        reporter_id=draft.reporter_id,
        reporter_name=draft.reporter_name,
        reporter_phone=draft.reporter_phone,
        reporter_email=draft.reporter_email,
        is_anonymous=draft.is_anonymous,
        contact_consent=draft.contact_consent,
        fingerprint=fingerprint,
        tags=list(dict.fromkeys(draft.tags)),
        extra=draft.extra,
        is_flagged=moderation.is_abusive or not moderation.is_acceptable,
        flag_reason=moderation.reason,
        visibility=Visibility.PUBLIC
        if tenant.feature_enabled("public_issues")
        else Visibility.INTERNAL,
    )
    session.add(issue)
    await session.flush()

    await _add_event(
        session,
        issue,
        IssueEventType.CREATED,
        note=f"Reported via {draft.channel.value}.",
        payload={"channel": str(draft.channel)},
    )

    # 4. Enrichment: embedding, triage, duplicates, routing, SLA.
    embedding = await _embed(issue, usage=usage)
    category = await routing_service.match_category(session, tenant.id, draft.category_slug)
    ai_applied = False

    if run_ai and tenant.feature_enabled("ai_triage"):
        ai_applied, category = await _apply_triage(session, tenant, issue, category, usage=usage)

    verdict = await duplicate_service.find_duplicates(
        session,
        tenant.id,
        title=issue.title,
        description=issue.description,
        category_id=category.id if category else None,
        latitude=latitude,
        longitude=longitude,
        embedding=embedding,
        exclude_id=issue.id,
        use_model=run_ai,
        usage=usage,
    )

    if (target := verdict.auto_merge_target) is not None:
        await merge_duplicate(session, issue, target, reason="Automatic duplicate detection")
        ISSUES_CREATED.labels(
            tenant=tenant.slug,
            category=category.slug if category else "unknown",
            channel=str(draft.channel),
        ).inc()
        return IntakeOutcome(
            issue=issue,
            created=True,
            merged_into=target,
            duplicate_candidates=[_candidate_payload(c) for c in verdict.candidates],
            ai_applied=ai_applied,
            warnings=[*warnings, f"Merged into existing report {target.reference}."],
        )

    if verdict.best is not None and verdict.best.needs_review:
        issue.extra["possible_duplicate_of"] = str(verdict.best.issue.id)
        warnings.append(
            f"Possibly a duplicate of {verdict.best.issue.reference} - flagged for review."
        )

    decision = await routing_service.route_issue(
        session,
        tenant.id,
        issue,
        category=category,
        suggested_department_slug=issue.ai_analysis.get("suggested_department_slug"),
        auto_assign=auto_assign and tenant.feature_enabled("auto_assignment"),
    )
    issue.category_id = category.id if category else None
    issue.department_id = decision.department_id
    issue.admin_unit_id = decision.admin_unit_id
    # Populate the relationships too: we already hold these objects, and doing
    # so means nothing downstream has to lazy-load them off a live session.
    if category is not None:
        issue.category = category

    if category is not None and draft.priority is None and not ai_applied:
        issue.priority = category.default_priority
    if category is not None and category.is_emergency:
        issue.priority = Priority.EMERGENCY

    department = (
        await session.get(Department, decision.department_id) if decision.department_id else None
    )
    if department is not None:
        issue.department = department
    targets = await sla_service.compute_targets(
        session,
        tenant.id,
        category_id=issue.category_id,
        priority=issue.priority,
        department=department,
        municipality=tenant,
        start=issue.created_at,
    )
    sla_service.apply_targets(issue, targets)

    if decision.assignee_id:
        await _assign(
            session, issue, decision.assignee_id, note=decision.reason, notify_assignee=False
        )
        issue.status = IssueStatus.ASSIGNED
    elif issue.category_id:
        issue.status = IssueStatus.TRIAGED

    if draft.reporter_id:
        await _follow(session, issue, draft.reporter_id, confirmed=True)
        await _bump_reporter_stats(session, draft.reporter_id)

    await session.flush()

    ISSUES_CREATED.labels(
        tenant=tenant.slug,
        category=category.slug if category else "unknown",
        channel=str(draft.channel),
    ).inc()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        entity_type="issue",
        entity_id=issue.id,
        entity_label=issue.reference,
        summary=f"Report {issue.reference} created via {draft.channel.value}",
        after={"reference": issue.reference, "status": str(issue.status)},
        tenant_id=tenant.id,
    )
    await _notify_reporter(session, tenant, issue, "issue.created")

    logger.info(
        "issue_created",
        reference=issue.reference,
        category=category.slug if category else None,
        priority=str(issue.priority),
        ai_applied=ai_applied,
    )
    return IntakeOutcome(
        issue=issue,
        created=True,
        duplicate_candidates=[_candidate_payload(c) for c in verdict.candidates],
        ai_applied=ai_applied,
        routing_reason=decision.describe(),
        warnings=warnings,
    )


# ------------------------------------------------------------- transitions ---


async def transition(
    session: AsyncSession,
    tenant: Municipality,
    issue: Issue,
    new_status: IssueStatus,
    *,
    note: str | None = None,
    actor: User | None = None,
    resolution_note: str | None = None,
    rejection_reason: str | None = None,
) -> Issue:
    """Move an issue through the workflow, enforcing the state machine."""
    current = issue.status
    if new_status is current:
        return issue

    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if new_status not in allowed:
        raise WorkflowError(
            f"Cannot move a report from '{current.value}' to '{new_status.value}'.",
            details={
                "from": current.value,
                "to": new_status.value,
                "allowed": sorted(status.value for status in allowed),
            },
        )
    if new_status is IssueStatus.REJECTED and not (rejection_reason or note):
        raise ValidationError(
            "A reason is required when closing a report without action.",
            code="rejection_reason_required",
        )

    before = audit_service.snapshot(issue, _TRACKED_FIELDS)
    now = utcnow()
    issue.status = new_status

    # First human response stops the response clock, whatever the status.
    if issue.first_response_at is None and new_status in _RESPONSE_STATUSES:
        issue.first_response_at = now

    if new_status is IssueStatus.RESOLVED:
        issue.resolved_at = now
        issue.resolution_note = resolution_note or note
    elif new_status is IssueStatus.VERIFIED:
        issue.verified_at = now
    elif new_status is IssueStatus.CLOSED:
        issue.closed_at = now
        if issue.resolved_at is None:
            issue.resolved_at = now
    elif new_status is IssueStatus.REJECTED:
        issue.closed_at = now
        issue.rejection_reason = rejection_reason or note
    elif new_status is IssueStatus.REOPENED:
        issue.reopened_count += 1
        issue.resolved_at = None
        issue.closed_at = None
        issue.verified_at = None
        issue.sla_resolution_state = SLAState.ON_TRACK

    await sla_service.refresh(session, issue)
    await _add_event(
        session,
        issue,
        IssueEventType.REOPENED
        if new_status is IssueStatus.REOPENED
        else IssueEventType.STATUS_CHANGED,
        from_value=current.value,
        to_value=new_status.value,
        note=note,
        actor=actor,
    )
    ISSUE_TRANSITIONS.labels(
        tenant=tenant.slug, from_status=current.value, to_status=new_status.value
    ).inc()

    await audit_service.record_change(
        session,
        entity_type="issue",
        entity_id=issue.id,
        entity_label=issue.reference,
        before=before,
        after=audit_service.snapshot(issue, _TRACKED_FIELDS),
        summary=f"{issue.reference}: {current.value} -> {new_status.value}",
    )

    template = {
        IssueStatus.ASSIGNED: "issue.assigned",
        IssueStatus.IN_PROGRESS: "issue.in_progress",
        IssueStatus.RESOLVED: "issue.resolved",
        IssueStatus.REJECTED: "issue.rejected",
    }.get(new_status)
    if template:
        await _notify_reporter(
            session,
            tenant,
            issue,
            template,
            extra_params={"reason": issue.rejection_reason or note or ""},
        )

    await session.flush()
    return issue


async def assign(
    session: AsyncSession,
    tenant: Municipality,
    issue: Issue,
    assignee_id: uuid.UUID,
    *,
    note: str | None = None,
    actor: User | None = None,
) -> Issue:
    """Assign an issue to a member of staff and advance it if it was untriaged."""
    assignee = await session.get(User, assignee_id)
    if assignee is None or assignee.tenant_id != tenant.id:
        raise ValidationError(
            "That user does not belong to this municipality.", code="bad_assignee"
        )

    await _assign(session, issue, assignee_id, note=note, actor=actor)
    if issue.status in {IssueStatus.SUBMITTED, IssueStatus.TRIAGED, IssueStatus.ACKNOWLEDGED}:
        await transition(session, tenant, issue, IssueStatus.ASSIGNED, note=note, actor=actor)
    return issue


async def merge_duplicate(
    session: AsyncSession,
    duplicate: Issue,
    parent: Issue,
    *,
    reason: str = "",
    actor: User | None = None,
) -> Issue:
    """Fold ``duplicate`` into ``parent``.

    The duplicate is kept (never deleted) so the reporter can still track their
    reference and be notified when the parent is resolved.
    """
    if duplicate.id == parent.id:
        raise ValidationError("A report cannot be merged into itself.", code="self_merge")

    duplicate.status = IssueStatus.DUPLICATE
    duplicate.duplicate_of_id = parent.id
    duplicate.closed_at = utcnow()
    parent.duplicate_count += 1
    parent.confirmations += 1

    # Corroboration is a prioritisation signal: many voices, one problem.
    if parent.confirmations >= 5 and parent.priority is Priority.NORMAL:
        parent.priority = Priority.HIGH
        await _add_event(
            session,
            parent,
            IssueEventType.PRIORITY_CHANGED,
            from_value=Priority.NORMAL.value,
            to_value=Priority.HIGH.value,
            note=f"Raised automatically after {parent.confirmations} corroborating reports.",
        )

    await _add_event(
        session,
        duplicate,
        IssueEventType.MERGED,
        to_value=parent.reference,
        note=reason or f"Merged into {parent.reference}.",
        actor=actor,
    )
    await _add_event(
        session,
        parent,
        IssueEventType.COMMENTED,
        note=f"Another resident reported the same problem ({duplicate.reference}).",
        is_public=True,
    )
    await session.flush()
    return duplicate


async def add_comment(
    session: AsyncSession,
    issue: Issue,
    body: str,
    *,
    author: User | None = None,
    visibility: Visibility = Visibility.PUBLIC,
    is_official: bool = False,
) -> IssueComment:
    body = normalise_whitespace(body)
    if not body:
        raise ValidationError("A comment cannot be empty.", code="empty_comment")

    moderation = await moderate_text(body, use_model=False)
    comment = IssueComment(
        tenant_id=issue.tenant_id,
        issue_id=issue.id,
        author_id=author.id if author else None,
        author_label=author.display() if author else "Resident",
        body=moderation.redacted_text,
        visibility=visibility,
        is_official=is_official,
        is_flagged=moderation.is_abusive,
    )
    session.add(comment)

    if visibility is Visibility.PUBLIC and issue.first_response_at is None and is_official:
        issue.first_response_at = utcnow()

    await _add_event(
        session,
        issue,
        IssueEventType.COMMENTED,
        note=truncate(body, 200),
        actor=author,
        is_public=visibility is Visibility.PUBLIC,
    )
    await session.flush()
    return comment


async def rate_resolution(
    session: AsyncSession,
    issue: Issue,
    rating: int,
    *,
    comment: str | None = None,
    rater_id: uuid.UUID | None = None,
) -> Issue:
    """Record the reporter's satisfaction with the outcome."""
    if not 1 <= rating <= 5:
        raise ValidationError("Rating must be between 1 and 5.", code="invalid_rating")
    if issue.status not in {IssueStatus.RESOLVED, IssueStatus.VERIFIED, IssueStatus.CLOSED}:
        raise WorkflowError("Only a resolved report can be rated.", code="not_resolved")
    if rater_id is not None and issue.reporter_id is not None and rater_id != issue.reporter_id:
        raise PermissionDeniedError("Only the reporter can rate this report.")

    issue.satisfaction_rating = rating
    issue.satisfaction_comment = comment
    await _add_event(
        session,
        issue,
        IssueEventType.RATED,
        to_value=str(rating),
        note=comment,
        is_public=False,
    )

    # A low score on a "resolved" job is the strongest signal we get that the
    # fix did not hold; surface it rather than closing the loop silently.
    if rating <= 2 and issue.status is not IssueStatus.CLOSED:
        issue.is_flagged = True
        issue.flag_reason = "Reporter was dissatisfied with the resolution."

    await session.flush()
    return issue


async def escalate(
    session: AsyncSession,
    tenant: Municipality,
    issue: Issue,
    *,
    reason: str,
    actor: User | None = None,
) -> Issue:
    """Raise an issue up the department's escalation chain."""
    issue.escalation_level += 1
    issue.escalated_at = utcnow()
    department = await session.get(Department, issue.department_id) if issue.department_id else None
    targets = sla_service.escalation_targets(department, issue.escalation_level)

    await _add_event(
        session,
        issue,
        IssueEventType.ESCALATED,
        to_value=str(issue.escalation_level),
        note=reason,
        actor=actor,
    )

    if targets:
        recipients = [
            Recipient.for_user(user)
            for user in (await session.scalars(select(User).where(User.id.in_(targets)))).all()
        ]
        await notify(
            session,
            tenant.id,
            recipients,
            template_key="issue.escalated",
            params={"reference": issue.reference, "level": issue.escalation_level},
            entity_type="issue",
            entity_id=issue.id,
        )
    await session.flush()
    return issue


async def follow(
    session: AsyncSession, issue: Issue, user_id: uuid.UUID, *, confirmed: bool = True
) -> IssueFollower:
    """ "Me too" - the corroboration signal that drives prioritisation."""
    follower = await _follow(session, issue, user_id, confirmed=confirmed)
    await session.flush()
    return follower


# ---------------------------------------------------------------- internals ---


async def _apply_triage(
    session: AsyncSession,
    tenant: Municipality,
    issue: Issue,
    category: IssueCategory | None,
    *,
    usage: UsageContext,
) -> tuple[bool, IssueCategory | None]:
    """Run AI triage and apply it only when it is confident and unambiguous."""
    taxonomy = await routing_service.taxonomy_for_prompt(session, tenant.id)
    try:
        result = await triage_report(
            title=issue.title,
            description=issue.description,
            categories=taxonomy,
            location=issue.address or issue.landmark,
            channel=str(issue.channel),
            reported_at=issue.created_at.isoformat() if issue.created_at else None,
            usage=usage,
        )
    except Exception as exc:
        logger.warning("triage_failed", reference=issue.reference, error=str(exc))
        return False, category

    # Always record what the model thought, even when we do not act on it.
    issue.ai_category_slug = result.category_slug
    issue.ai_confidence = result.confidence
    issue.ai_priority = Priority(result.priority)
    issue.ai_severity = Severity(result.severity)
    issue.ai_summary = result.summary
    issue.ai_triaged_at = utcnow()
    issue.ai_analysis = {
        "reasoning": result.reasoning,
        "tags": result.tags,
        "is_emergency": result.is_emergency,
        "requires_field_visit": result.requires_field_visit,
        "suggested_department_slug": result.suggested_department_slug,
        "missing_information": result.missing_information,
    }

    threshold = float(tenant.setting("ai_triage_confidence", 0.5))
    if not is_confident(result, threshold):
        await _add_event(
            session,
            issue,
            IssueEventType.AI_TRIAGED,
            to_value=result.category_slug,
            note=(
                f"Suggested '{result.category_slug}' at {result.confidence:.0%} confidence - "
                "below the threshold, so manual triage is required."
            ),
            is_public=False,
            payload={"applied": False, "confidence": result.confidence},
        )
        return False, category

    # A human-chosen category always wins over the model's.
    if category is None:
        category = await routing_service.match_category(session, tenant.id, result.category_slug)
    if issue.severity is None:
        issue.severity = Severity(result.severity)
    if result.is_emergency:
        issue.priority = Priority.EMERGENCY
    else:
        issue.priority = Priority(result.priority)
    if result.title and issue.title == summarise_for_title(issue.description):
        issue.title = truncate(result.title, 255)
    issue.tags = list(dict.fromkeys([*issue.tags, *result.tags]))[:10]

    await _add_event(
        session,
        issue,
        IssueEventType.AI_TRIAGED,
        to_value=result.category_slug,
        note=result.reasoning,
        is_public=False,
        payload={"applied": True, "confidence": result.confidence},
    )
    return True, category


async def _embed(issue: Issue, *, usage: UsageContext) -> list[float] | None:
    try:
        result = await run_embedding([f"{issue.title}\n{issue.description}"], usage=usage)
        if result.vectors:
            issue.embedding = result.vectors[0]
            return result.vectors[0]
    except Exception as exc:
        logger.warning("issue_embedding_failed", reference=issue.reference, error=str(exc))
    return None


async def _assign(
    session: AsyncSession,
    issue: Issue,
    assignee_id: uuid.UUID,
    *,
    note: str | None = None,
    actor: User | None = None,
    notify_assignee: bool = True,
) -> None:
    previous = issue.assigned_to_id
    issue.assigned_to_id = assignee_id
    issue.assigned_at = utcnow()
    issue.assigned_by_id = actor.id if actor else context.get_actor().id

    await _add_event(
        session,
        issue,
        IssueEventType.ASSIGNED,
        from_value=str(previous) if previous else None,
        to_value=str(assignee_id),
        note=note,
        actor=actor,
        is_public=False,
    )

    if notify_assignee:
        assignee = await session.get(User, assignee_id)
        if assignee is not None:
            await notify(
                session,
                issue.tenant_id,
                [Recipient.for_user(assignee)],
                template_key="issue.assigned",
                params={
                    "reference": issue.reference,
                    "department": assignee.designation or "your team",
                },
                entity_type="issue",
                entity_id=issue.id,
            )


async def _follow(
    session: AsyncSession, issue: Issue, user_id: uuid.UUID, *, confirmed: bool
) -> IssueFollower:
    existing = await session.scalar(
        select(IssueFollower).where(
            IssueFollower.issue_id == issue.id, IssueFollower.user_id == user_id
        )
    )
    if existing is not None:
        if confirmed and not existing.confirmed:
            existing.confirmed = True
            issue.confirmations += 1
        return existing

    follower = IssueFollower(
        tenant_id=issue.tenant_id,
        issue_id=issue.id,
        user_id=user_id,
        confirmed=confirmed,
    )
    session.add(follower)
    if confirmed and user_id != issue.reporter_id:
        issue.confirmations += 1
    return follower


async def _register_confirmation(
    session: AsyncSession, issue: Issue, user_id: uuid.UUID | None
) -> None:
    if user_id is not None:
        await _follow(session, issue, user_id, confirmed=True)
        await session.flush()


async def _add_event(
    session: AsyncSession,
    issue: Issue,
    event_type: IssueEventType,
    *,
    from_value: str | None = None,
    to_value: str | None = None,
    note: str | None = None,
    actor: User | None = None,
    is_public: bool = True,
    payload: dict[str, Any] | None = None,
) -> IssueEvent:
    current = context.get_actor()
    event = IssueEvent(
        tenant_id=issue.tenant_id,
        issue_id=issue.id,
        event_type=event_type,
        actor_id=actor.id if actor else current.id,
        actor_label=(
            actor.display() if actor else (current.display_name or current.email or current.kind)
        ),
        from_value=from_value,
        to_value=to_value,
        note=truncate(note, 2000) if note else None,
        is_public=is_public,
        payload=payload or {},
    )
    session.add(event)
    return event


async def _notify_reporter(
    session: AsyncSession,
    tenant: Municipality,
    issue: Issue,
    template_key: str,
    *,
    extra_params: dict[str, Any] | None = None,
) -> None:
    """Tell the reporter what happened, whether or not they have an account."""
    if not issue.contact_consent:
        return

    recipients: list[Recipient] = []
    if issue.reporter_id:
        reporter = await session.get(User, issue.reporter_id)
        if reporter is not None:
            recipients.append(Recipient.for_user(reporter))
    else:
        anonymous = recipient_from_contact(
            issue.reporter_phone, issue.reporter_email, issue.language
        )
        if anonymous is not None:
            recipients.append(anonymous)

    if not recipients:
        return

    department_name = "the relevant department"
    if issue.department_id:
        department = await session.get(Department, issue.department_id)
        if department is not None:
            department_name = department.name

    params = {
        "reference": issue.reference,
        "department": department_name,
        "parent": issue.duplicate_of_id or "",
        **(extra_params or {}),
    }
    await notify(
        session,
        tenant.id,
        recipients,
        template_key=template_key,
        params=params,
        subject=translate(template_key, issue.language, **params)[:120],
        entity_type="issue",
        entity_id=issue.id,
    )


async def _bump_reporter_stats(session: AsyncSession, user_id: uuid.UUID) -> None:
    user = await session.get(User, user_id)
    if user is not None:
        user.reports_submitted += 1


async def _unique_reference(
    repository: IssueRepository, tenant: Municipality, attempts: int = 5
) -> str:
    prefix = (tenant.slug[:3] or "civ").upper()
    for _ in range(attempts):
        reference = generate_reference(prefix)
        if await repository.get_by_reference(reference) is None:
            return reference
    # Astronomically unlikely; fall back to a longer code rather than collide.
    return generate_reference(prefix, length=9)


def _validated_location(
    draft: IssueDraft, warnings: list[str]
) -> tuple[float | None, float | None]:
    if draft.latitude is None or draft.longitude is None:
        return None, None
    if not is_valid_coordinate(draft.latitude, draft.longitude):
        warnings.append("The supplied coordinates were invalid and have been discarded.")
        return None, None
    return draft.latitude, draft.longitude


def _candidate_payload(candidate: duplicate_service.DuplicateCandidate) -> dict[str, Any]:
    return {
        "issue_id": str(candidate.issue.id),
        "reference": candidate.issue.reference,
        "title": candidate.issue.title,
        "status": str(candidate.issue.status),
        "distance_meters": candidate.distance_meters,
        "score": candidate.combined_score,
        "reason": candidate.reason,
    }


async def nearby_issues(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    point: Point,
    radius_meters: int = 500,
    limit: int = 20,
) -> Sequence[tuple[Issue, float]]:
    """Public "what's already reported near me" lookup."""
    repository = IssueRepository(session, tenant_id)
    return await repository.find_nearby(point, radius_meters, limit=limit)
