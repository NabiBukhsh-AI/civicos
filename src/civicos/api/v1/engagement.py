"""Engagement: announcements, emergency alerts, surveys, feedback, notifications."""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select

from civicos.ai.language import analyse_sentiment, translate_text
from civicos.ai.usage import UsageContext
from civicos.api.deps import (
    CurrentUserDep,
    OptionalActorDep,
    PageDep,
    SessionDep,
    TenantDep,
    require_permission,
)
from civicos.core.clock import utcnow
from civicos.core.errors import ConflictError, NotFoundError, ValidationError
from civicos.core.pagination import Page as PageResult
from civicos.core.permissions import Resource, perm
from civicos.domain.engagement import (
    Announcement,
    EmergencyAlert,
    Feedback,
    Survey,
    SurveyQuestion,
    SurveyResponse,
)
from civicos.domain.enums import (
    NotificationChannel,
    QuestionType,
    SurveyStatus,
    Visibility,
)
from civicos.domain.identity import User
from civicos.schemas.common import Message, Page
from civicos.schemas.operations import (
    AlertOut,
    AlertRequest,
    AnnouncementOut,
    AnnouncementRequest,
    FeedbackRequest,
    NotificationOut,
    SurveyOut,
    SurveyRequest,
    SurveyResponseRequest,
)
from civicos.services import notification_service
from civicos.services.notification_service import Recipient, notify

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Engagement"])


# ----------------------------------------------------------- announcements ---


@router.post(
    "/announcements",
    response_model=AnnouncementOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(Resource.ANNOUNCEMENT, "create")))],
)
async def create_announcement(
    payload: AnnouncementRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> AnnouncementOut:
    """Publish a notice, tender, event or planned-outage announcement."""
    announcement = Announcement(
        tenant_id=tenant.id,
        title=payload.title,
        summary=payload.summary,
        body=payload.body,
        language=payload.language,
        announcement_type=payload.announcement_type,
        department_id=payload.department_id,
        admin_unit_ids=[str(unit) for unit in payload.admin_unit_ids],
        visibility=payload.visibility,
        is_pinned=payload.is_pinned,
        published_at=utcnow() if payload.publish_now else None,
        expires_at=payload.expires_at,
        event_starts_at=payload.event_starts_at,
        event_ends_at=payload.event_ends_at,
        location=payload.location,
        author_id=user.id,
    )
    session.add(announcement)
    await session.flush()

    if payload.auto_translate:
        announcement.body_translations = await _translate_body(
            session, tenant, payload.body, payload.language
        )

    await session.commit()
    return AnnouncementOut.model_validate(announcement)


@router.get("/announcements", response_model=Page[AnnouncementOut])
async def list_announcements(
    session: SessionDep, tenant: TenantDep, page: PageDep, actor: OptionalActorDep
) -> Page[AnnouncementOut]:
    statement = select(Announcement).where(
        Announcement.tenant_id == tenant.id, Announcement.deleted_at.is_(None)
    )
    if not actor.is_authenticated:
        statement = statement.where(Announcement.visibility == Visibility.PUBLIC)

    total = int(await session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = (
        await session.scalars(
            statement.order_by(Announcement.is_pinned.desc(), Announcement.created_at.desc())
            .offset(page.offset)
            .limit(page.limit)
        )
    ).all()
    result = PageResult.build([AnnouncementOut.model_validate(row) for row in rows], total, page)
    return Page[AnnouncementOut].model_validate(result.model_dump())


# ------------------------------------------------------------------ alerts ---


@router.post(
    "/alerts",
    response_model=AlertOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(Resource.ALERT, "create")))],
)
async def create_alert(
    payload: AlertRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> AlertOut:
    """Issue an emergency alert and fan it out to the configured channels.

    Alerts are geo-targetable (ward list or radius) so a localised flood warning
    does not have to reach the whole municipality.
    """
    alert = EmergencyAlert(
        tenant_id=tenant.id,
        title=payload.title,
        message=payload.message,
        instructions=payload.instructions,
        severity=payload.severity,
        category=payload.category,
        admin_unit_ids=[str(unit) for unit in payload.admin_unit_ids],
        centre_latitude=payload.centre_latitude,
        centre_longitude=payload.centre_longitude,
        radius_meters=payload.radius_meters,
        channels=payload.channels,
        expires_at=payload.expires_at,
        issued_by_id=user.id,
    )
    session.add(alert)
    await session.flush()

    if payload.issue_now:
        await _broadcast(session, tenant, alert)
    await session.commit()
    return AlertOut.model_validate(alert)


@router.post(
    "/alerts/{alert_id}/cancel",
    response_model=AlertOut,
    dependencies=[Depends(require_permission(perm(Resource.ALERT, "create")))],
)
async def cancel_alert(
    alert_id: uuid.UUID,
    session: SessionDep,
    tenant: TenantDep,
    note: Annotated[str | None, Query(max_length=500)] = None,
) -> AlertOut:
    """Stand down an alert. Cancelling explicitly matters as much as issuing."""
    alert = await session.get(EmergencyAlert, alert_id)
    if alert is None or alert.tenant_id != tenant.id:
        raise NotFoundError("Alert not found.", code="alert_not_found")
    alert.is_active = False
    alert.cancelled_at = utcnow()
    alert.cancellation_note = note
    await session.commit()
    return AlertOut.model_validate(alert)


async def _broadcast(session, tenant, alert: EmergencyAlert) -> None:
    """Send an alert to residents in the targeted area."""
    statement = select(User).where(
        User.tenant_id == tenant.id,
        User.deleted_at.is_(None),
        User.status == "active",
    )
    if alert.admin_unit_ids:
        statement = statement.where(
            User.admin_unit_id.in_([uuid.UUID(str(u)) for u in alert.admin_unit_ids])
        )
    recipients = [Recipient.for_user(user) for user in (await session.scalars(statement)).all()]

    channels = [
        NotificationChannel(channel)
        for channel in alert.channels
        if channel in set(NotificationChannel)
    ]
    notifications = await notify(
        session,
        tenant.id,
        recipients,
        template_key="alert.emergency",
        params={"message": alert.message},
        subject=alert.title,
        body=f"{alert.title}\n\n{alert.message}"
        + (f"\n\n{alert.instructions}" if alert.instructions else ""),
        channels=channels or None,
        entity_type="emergency_alert",
        entity_id=alert.id,
    )
    alert.is_active = True
    alert.issued_at = utcnow()
    alert.recipients_targeted = len(recipients)
    alert.recipients_delivered = sum(
        1 for item in notifications if item.status.value in {"sent", "delivered"}
    )


# ----------------------------------------------------------------- surveys ---


@router.post(
    "/surveys",
    response_model=SurveyOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(Resource.SURVEY, "create")))],
)
async def create_survey(
    payload: SurveyRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> SurveyOut:
    survey = Survey(
        tenant_id=tenant.id,
        title=payload.title,
        description=payload.description,
        admin_unit_ids=[str(unit) for unit in payload.admin_unit_ids],
        opens_at=payload.opens_at,
        closes_at=payload.closes_at,
        is_anonymous=payload.is_anonymous,
        requires_verification=payload.requires_verification,
        status=SurveyStatus.OPEN if payload.opens_at is None else SurveyStatus.DRAFT,
        created_by_id=user.id,
    )
    session.add(survey)
    await session.flush()

    for index, question in enumerate(payload.questions):
        session.add(
            SurveyQuestion(
                tenant_id=tenant.id,
                survey_id=survey.id,
                prompt=question.prompt,
                question_type=QuestionType(question.question_type),
                options=question.options,
                is_required=question.is_required,
                display_order=question.display_order or index,
            )
        )
    await session.commit()
    await session.refresh(survey)
    return SurveyOut.model_validate(survey)


@router.get("/surveys", response_model=list[SurveyOut])
async def list_surveys(
    session: SessionDep, tenant: TenantDep, actor: OptionalActorDep
) -> list[SurveyOut]:
    statement = select(Survey).where(Survey.tenant_id == tenant.id, Survey.deleted_at.is_(None))
    if not actor.is_authenticated:
        statement = statement.where(Survey.status == SurveyStatus.OPEN)
    rows = (await session.scalars(statement.order_by(Survey.created_at.desc()))).unique().all()
    return [SurveyOut.model_validate(row) for row in rows]


@router.post("/surveys/{survey_id}/responses", response_model=Message, status_code=201)
async def submit_response(
    survey_id: uuid.UUID,
    payload: SurveyResponseRequest,
    session: SessionDep,
    tenant: TenantDep,
    actor: OptionalActorDep,
) -> Message:
    """Submit a survey response.

    Anonymous surveys still reject double-voting by hashing the respondent's
    identity rather than storing it.
    """
    survey = await session.get(Survey, survey_id)
    if survey is None or survey.tenant_id != tenant.id:
        raise NotFoundError("Survey not found.", code="survey_not_found")
    if not survey.is_open:
        raise ValidationError("This survey is not currently open.", code="survey_closed")
    if survey.requires_verification and not actor.is_authenticated:
        raise ValidationError(
            "This survey is open to verified residents only.", code="verification_required"
        )

    respondent_key = _respondent_key(tenant.id, survey_id, actor.id)
    if await session.scalar(
        select(SurveyResponse).where(
            SurveyResponse.survey_id == survey_id,
            SurveyResponse.respondent_key == respondent_key,
        )
    ):
        raise ConflictError("You have already responded to this survey.", code="already_responded")

    session.add(
        SurveyResponse(
            tenant_id=tenant.id,
            survey_id=survey_id,
            user_id=actor.id if (actor.is_authenticated and not survey.is_anonymous) else None,
            respondent_key=respondent_key,
            admin_unit_id=payload.admin_unit_id,
            answers=payload.answers,
        )
    )
    survey.response_count += 1
    await session.commit()
    return Message(message="Thank you - your response has been recorded.")


@router.get(
    "/surveys/{survey_id}/results",
    response_model=dict,
    dependencies=[Depends(require_permission(perm(Resource.SURVEY, "read")))],
)
async def survey_results(
    survey_id: uuid.UUID, session: SessionDep, tenant: TenantDep
) -> dict[str, Any]:
    """Tally responses per question."""
    survey = await session.get(Survey, survey_id)
    if survey is None or survey.tenant_id != tenant.id:
        raise NotFoundError("Survey not found.", code="survey_not_found")

    responses = (
        await session.scalars(select(SurveyResponse).where(SurveyResponse.survey_id == survey_id))
    ).all()

    tallies: dict[str, dict[str, int]] = {}
    free_text: dict[str, list[str]] = {}
    for response in responses:
        for question_id, answer in (response.answers or {}).items():
            if isinstance(answer, str) and len(answer) > 60:
                free_text.setdefault(question_id, []).append(answer)
                continue
            values = answer if isinstance(answer, list) else [answer]
            bucket = tallies.setdefault(question_id, {})
            for value in values:
                bucket[str(value)] = bucket.get(str(value), 0) + 1

    return {
        "survey_id": str(survey.id),
        "title": survey.title,
        "status": str(survey.status),
        "response_count": len(responses),
        "questions": [
            {
                "id": str(question.id),
                "prompt": question.prompt,
                "type": str(question.question_type),
                "tally": tallies.get(str(question.id), {}),
                "free_text_count": len(free_text.get(str(question.id), [])),
            }
            for question in survey.questions
        ],
        "ai_summary": survey.ai_summary,
    }


# ---------------------------------------------------------------- feedback ---


@router.post("/feedback", response_model=Message, status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    payload: FeedbackRequest,
    session: SessionDep,
    tenant: TenantDep,
    actor: OptionalActorDep,
) -> Message:
    """Feedback about the service itself, with automatic sentiment tagging."""
    analysis = await analyse_sentiment(
        payload.body, usage=UsageContext.from_request(session, tenant_id=tenant.id)
    )
    session.add(
        Feedback(
            tenant_id=tenant.id,
            user_id=actor.id if actor.kind == "user" else None,
            subject=payload.subject,
            body=payload.body,
            rating=payload.rating,
            sentiment=analysis.sentiment,
            sentiment_score=analysis.score,
            themes=analysis.themes,
            contact_email=payload.contact_email,
        )
    )
    await session.commit()
    return Message(message="Thank you for your feedback.")


# ----------------------------------------------------------- notifications ---


@router.get("/notifications", response_model=Page[NotificationOut])
async def my_notifications(
    session: SessionDep,
    tenant: TenantDep,
    page: PageDep,
    user: CurrentUserDep,
    unread_only: Annotated[bool, Query()] = False,
) -> Page[NotificationOut]:
    rows, total = await notification_service.list_for_user(
        session, tenant.id, user.id, unread_only=unread_only, page=page
    )
    result = PageResult.build([NotificationOut.model_validate(row) for row in rows], total, page)
    return Page[NotificationOut].model_validate(result.model_dump())


@router.get("/notifications/unread-count", response_model=dict)
async def unread_count(
    session: SessionDep, tenant: TenantDep, user: CurrentUserDep
) -> dict[str, int]:
    return {"unread": await notification_service.unread_count(session, tenant.id, user.id)}


@router.post("/notifications/read", response_model=Message)
async def mark_read(
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
    notification_ids: list[uuid.UUID] | None = None,
) -> Message:
    count = await notification_service.mark_read(session, tenant.id, user.id, notification_ids)
    await session.commit()
    return Message(message=f"{count} notification(s) marked as read.")


# ---------------------------------------------------------------- helpers ----


def _respondent_key(tenant_id: uuid.UUID, survey_id: uuid.UUID, user_id: uuid.UUID | None) -> str:
    """One-way identity hash so anonymity and one-vote-per-person coexist."""
    from civicos.core.config import get_settings

    seed = str(user_id) if user_id else uuid.uuid4().hex
    secret = get_settings().security.secret_key
    return hashlib.sha256(f"{secret}:{tenant_id}:{survey_id}:{seed}".encode()).hexdigest()


async def _translate_body(session, tenant, body: str, source_language: str) -> dict[str, str]:
    """Pre-translate a notice into the tenant's other languages."""
    translations: dict[str, str] = {}
    for code in tenant.supported_languages or []:
        if code == source_language:
            continue
        try:
            result = await translate_text(
                body,
                code,
                source_language=source_language,
                usage=UsageContext.from_request(session, tenant_id=tenant.id),
            )
            translations[code] = result.translated_text
        except Exception as exc:  # a failed translation must not block publication
            logger.warning("announcement_translation_failed", language=code, error=str(exc))
            continue
    return translations
