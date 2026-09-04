"""AI endpoints: the assistant, vision analysis, triage preview, translation."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from sqlalchemy import select

from civicos.ai import assistant, briefing, vision
from civicos.ai import language as language_ai
from civicos.ai.rag.retriever import audience_for_role
from civicos.ai.registry import is_ai_enabled
from civicos.ai.triage import DEFAULT_CONFIDENCE_FLOOR, is_confident, triage_report
from civicos.ai.types import ImagePart
from civicos.ai.usage import UsageContext, tenant_usage_summary
from civicos.api.deps import (
    CurrentUserDep,
    LanguageDep,
    OptionalActorDep,
    SessionDep,
    TenantDep,
    ai_rate_limit,
    require_permission,
)
from civicos.core.errors import NotFoundError, ValidationError
from civicos.core.geo import Point
from civicos.core.permissions import Resource, is_staff, perm
from civicos.core.text import truncate
from civicos.domain.enums import Visibility
from civicos.domain.knowledge import Conversation, ConversationMessage
from civicos.integrations.exif import extract_metadata, location_consistency
from civicos.integrations.storage import verify_declared_type
from civicos.schemas.ai import (
    AIUsageOut,
    AskRequest,
    AskResponse,
    BriefingRequest,
    BriefingResponse,
    BriefingSectionOut,
    CitationOut,
    ConversationMessageOut,
    ConversationOut,
    ImageMetadataOut,
    MessageFeedbackRequest,
    TranslateRequest,
    TranslateResponse,
    TriagePreviewRequest,
    TriagePreviewResponse,
    VisionAnalysisResponse,
    VisionFindingOut,
)
from civicos.schemas.common import Message
from civicos.services.routing_service import taxonomy_for_prompt

router = APIRouter(prefix="/assistant", tags=["AI Assistant"])


@router.post(
    "/ask",
    response_model=AskResponse,
    dependencies=[Depends(ai_rate_limit("assistant"))],
)
async def ask(
    payload: AskRequest,
    session: SessionDep,
    tenant: TenantDep,
    actor: OptionalActorDep,
    language: LanguageDep,
) -> AskResponse:
    """Ask a question about this municipality.

    Answers are grounded in the tenant's indexed documents and carry citations.
    Residents see only public documents; staff see the levels their role allows.
    """
    if not tenant.feature_enabled("assistant"):
        raise NotFoundError("The assistant is not enabled for this municipality.")

    audience = audience_for_role(actor.role, is_staff(actor.role or ""))
    conversation = await assistant.get_or_create_conversation(
        session,
        tenant.id,
        conversation_id=payload.conversation_id,
        session_key=payload.session_key,
        user_id=actor.id if actor.kind == "user" else None,
        language=payload.language or language,
        audience=audience,
    )
    reply = await assistant.ask(
        session,
        tenant.id,
        payload.question,
        audience=audience,
        conversation=conversation,
        language=payload.language or language,
        document_ids=payload.document_ids or None,
        usage=UsageContext.from_request(session, tenant_id=tenant.id),
    )
    await session.commit()

    return AskResponse(
        answer=reply.answer,
        citations=[CitationOut(**citation) for citation in reply.citations],
        conversation_id=reply.conversation_id,
        message_id=reply.message_id,
        grounded=reply.grounded,
        grounding_score=reply.grounding_score,
        confidence=reply.confidence,
        escalate_to_human=reply.escalate_to_human,
        follow_up_questions=reply.follow_up_questions,
        model=reply.model,
        provider=reply.provider,
        latency_ms=reply.latency_ms,
    )


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    session: SessionDep, tenant: TenantDep, user: CurrentUserDep
) -> list[ConversationOut]:
    rows = (
        await session.scalars(
            select(Conversation)
            .where(
                Conversation.tenant_id == tenant.id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            )
            .order_by(Conversation.last_message_at.desc().nullslast())
            .limit(50)
        )
    ).all()
    return [ConversationOut.model_validate(row) for row in rows]


@router.get("/conversations/{conversation_id}", response_model=list[ConversationMessageOut])
async def conversation_messages(
    conversation_id: uuid.UUID,
    session: SessionDep,
    tenant: TenantDep,
    actor: OptionalActorDep,
) -> list[ConversationMessageOut]:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.tenant_id != tenant.id:
        raise NotFoundError("Conversation not found.", code="conversation_not_found")
    if conversation.user_id and conversation.user_id != actor.id and not actor.is_superadmin:
        raise NotFoundError("Conversation not found.", code="conversation_not_found")

    rows = (
        await session.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.created_at.asc())
        )
    ).all()
    return [ConversationMessageOut.model_validate(row) for row in rows]


@router.post("/messages/{message_id}/feedback", response_model=Message)
async def rate_answer(
    message_id: uuid.UUID,
    payload: MessageFeedbackRequest,
    session: SessionDep,
    tenant: TenantDep,
) -> Message:
    """Thumbs up/down on an answer - the signal used to tune retrieval."""
    message = await session.get(ConversationMessage, message_id)
    if message is None or message.tenant_id != tenant.id:
        raise NotFoundError("Message not found.", code="message_not_found")
    message.feedback = payload.rating
    await session.commit()
    return Message(message="Thank you - your feedback helps improve the assistant.")


@router.post(
    "/vision",
    response_model=VisionAnalysisResponse,
    dependencies=[
        Depends(require_permission(perm(Resource.AI, "vision"))),
        Depends(ai_rate_limit("vision")),
    ],
)
async def analyse(
    session: SessionDep,
    tenant: TenantDep,
    files: Annotated[list[UploadFile], File(description="Site photographs.")],
    context: Annotated[str | None, Form(max_length=1000)] = None,
    asset_hint: Annotated[str | None, Form(max_length=200)] = None,
    latitude: Annotated[float | None, Form()] = None,
    longitude: Annotated[float | None, Form()] = None,
) -> VisionAnalysisResponse:
    """Analyse civic-issue photographs and extract their metadata.

    Returns a structured assessment - category, severity, hazards, recommended
    actions, a crew-hours estimate - alongside per-image EXIF, and checks the
    photo GPS against a supplied reference location when one is given.
    """
    from civicos.core.config import get_settings

    settings = get_settings()
    if not files:
        raise ValidationError("At least one image is required.", code="no_images")

    parts: list[ImagePart] = []
    metadata_out: list[ImageMetadataOut] = []
    points: list[Point] = []

    for upload in files[: settings.ai.max_images_per_request]:
        data = await upload.read()
        content_type = verify_declared_type(data, upload.content_type or "image/jpeg")
        parts.append(
            ImagePart(data=data, media_type=content_type, label=upload.filename or "image")
        )
        metadata = extract_metadata(data, upload.filename or "image")
        metadata_out.append(ImageMetadataOut(**metadata.to_dict()))
        if metadata.point is not None:
            points.append(metadata.point)

    taxonomy = await taxonomy_for_prompt(session, tenant.id)
    report = await vision.analyse_images(
        parts,
        categories=taxonomy,
        context_note=context,
        asset_hint=asset_hint,
        usage=UsageContext.from_request(session, tenant_id=tenant.id),
    )
    await session.commit()

    reference = (
        Point(latitude, longitude) if latitude is not None and longitude is not None else None
    )
    return VisionAnalysisResponse(
        summary=report.summary,
        observations=report.observations,
        findings=[VisionFindingOut(**finding.model_dump()) for finding in report.findings],
        primary_category_slug=report.primary_category_slug,
        severity=report.severity,
        condition=report.condition,
        hazards=report.hazards,
        recommended_actions=report.recommended_actions,
        estimated_crew_hours=report.estimated_crew_hours,
        requires_specialist=report.requires_specialist,
        image_quality_note=report.image_quality_note,
        confidence=report.confidence,
        images=metadata_out,
        location_check=location_consistency(reference, points),
    )


@router.post(
    "/triage-preview",
    response_model=TriagePreviewResponse,
    dependencies=[
        Depends(require_permission(perm(Resource.AI, "triage"))),
        Depends(ai_rate_limit("triage")),
    ],
)
async def triage_preview(
    payload: TriagePreviewRequest, session: SessionDep, tenant: TenantDep
) -> TriagePreviewResponse:
    """See how a report would be classified, without filing it.

    Used by helpline operators to sanity-check routing, and by administrators
    tuning the category keywords.
    """
    taxonomy = await taxonomy_for_prompt(session, tenant.id)
    result = await triage_report(
        title=payload.title or truncate(payload.description, 80),
        description=payload.description,
        categories=taxonomy,
        location=payload.address,
        usage=UsageContext.from_request(session, tenant_id=tenant.id),
    )
    await session.commit()

    threshold = float(tenant.setting("ai_triage_confidence", DEFAULT_CONFIDENCE_FLOOR))
    return TriagePreviewResponse(
        **result.model_dump(), would_be_applied=is_confident(result, threshold)
    )


@router.post(
    "/translate",
    response_model=TranslateResponse,
    dependencies=[
        Depends(require_permission(perm(Resource.AI, "translate"))),
        Depends(ai_rate_limit("translate")),
    ],
)
async def translate(
    payload: TranslateRequest, session: SessionDep, tenant: TenantDep
) -> TranslateResponse:
    result = await language_ai.translate_text(
        payload.text,
        payload.target_language,
        source_language=payload.source_language,
        usage=UsageContext.from_request(session, tenant_id=tenant.id),
    )
    await session.commit()
    return TranslateResponse(**result.model_dump())


@router.post(
    "/briefing",
    response_model=BriefingResponse,
    dependencies=[
        Depends(require_permission(perm(Resource.AI, "brief"))),
        Depends(ai_rate_limit("briefing")),
    ],
)
async def executive_briefing(
    payload: BriefingRequest, session: SessionDep, tenant: TenantDep
) -> BriefingResponse:
    """Generate the operations briefing for the period.

    Statistics are computed in SQL; the model only composes and prioritises.
    """
    from civicos.services import analytics_service

    statistics = await analytics_service.dashboard(session, tenant.id, days=payload.days)
    statistics["hotspots"] = [
        hotspot.to_dict()
        for hotspot in await analytics_service.hotspots(session, tenant.id, days=payload.days)
    ][:5]

    result = await briefing.compose_briefing(
        statistics,
        period_label=f"last {payload.days} day(s)",
        municipality_name=tenant.name,
        audience=payload.audience,
        usage=UsageContext.from_request(session, tenant_id=tenant.id),
    )
    await session.commit()

    return BriefingResponse(
        headline=result.headline,
        key_points=result.key_points,
        sections=[BriefingSectionOut(**section.model_dump()) for section in result.sections],
        risks=result.risks,
        recommended_actions=result.recommended_actions,
        period_label=result.period_label,
        markdown=briefing.render_markdown(result),
    )


@router.get(
    "/usage",
    response_model=AIUsageOut,
    dependencies=[Depends(require_permission(perm(Resource.ANALYTICS, "read")))],
)
async def ai_usage(
    session: SessionDep,
    tenant: TenantDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> AIUsageOut:
    """AI spend for this municipality, by capability."""
    return AIUsageOut(**await tenant_usage_summary(session, tenant.id, days=days))


@router.get("/status", response_model=dict, status_code=status.HTTP_200_OK)
async def assistant_status(tenant: TenantDep) -> dict:
    """Whether AI features are live, so clients can hide what will not work."""
    from civicos.core.config import get_settings

    settings = get_settings()
    return {
        "ai_enabled": is_ai_enabled(),
        "provider": settings.ai.provider,
        "chat_model": settings.ai.chat_model if is_ai_enabled() else "heuristic-v1",
        "features": {
            "assistant": tenant.feature_enabled("assistant"),
            "ai_triage": tenant.feature_enabled("ai_triage"),
            "vision": tenant.feature_enabled("vision"),
        },
        "audience_default": Visibility.PUBLIC.value,
    }
