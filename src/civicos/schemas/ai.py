"""AI endpoint contracts: assistant, vision, triage preview, documents."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import Field

from civicos.domain.enums import DocumentStatus, DocumentType, Visibility
from civicos.schemas.common import APIModel, InputModel


class AskRequest(InputModel):
    question: str = Field(
        min_length=3,
        max_length=2000,
        examples=["What documents do I need for a trade licence?"],
    )
    conversation_id: uuid.UUID | None = Field(
        default=None, description="Continue an existing thread."
    )
    session_key: str | None = Field(
        default=None,
        max_length=64,
        description="Anonymous continuity key when there is no account.",
    )
    language: str | None = Field(default=None, max_length=8)
    document_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=20,
        description="Restrict retrieval to specific documents.",
    )


class CitationOut(APIModel):
    document_id: uuid.UUID
    title: str
    page: int | None = None
    section: str | None = None
    score: float | None = None
    quote: str | None = None


class AskResponse(APIModel):
    answer: str
    citations: list[CitationOut] = Field(default_factory=list)
    conversation_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    grounded: bool = Field(
        description="False means the answer was not supported by indexed documents."
    )
    grounding_score: float
    confidence: float
    escalate_to_human: bool
    follow_up_questions: list[str] = Field(default_factory=list)
    model: str | None = None
    provider: str | None = None
    latency_ms: int = 0


class MessageFeedbackRequest(InputModel):
    rating: int = Field(ge=-1, le=1, description="-1 unhelpful, 0 neutral, +1 helpful.")
    note: str | None = Field(default=None, max_length=1000)


class ConversationOut(APIModel):
    id: uuid.UUID
    title: str | None = None
    language: str
    message_count: int
    last_message_at: datetime | None = None
    created_at: datetime


class ConversationMessageOut(APIModel):
    id: uuid.UUID
    role: str
    content: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    grounding_score: float | None = None
    feedback: int | None = None
    created_at: datetime


class VisionFindingOut(APIModel):
    label: str
    category_slug: str
    severity: str
    confidence: float
    description: str


class ImageMetadataOut(APIModel):
    filename: str
    latitude: float | None = None
    longitude: float | None = None
    captured_at: datetime | None = None
    device: str | None = None
    width: int | None = None
    height: int | None = None
    has_exif: bool = False
    warnings: list[str] = Field(default_factory=list)


class VisionAnalysisResponse(APIModel):
    """Structured reading of site photographs, plus per-image metadata."""

    summary: str
    observations: list[str] = Field(default_factory=list)
    findings: list[VisionFindingOut] = Field(default_factory=list)
    primary_category_slug: str
    severity: str
    condition: str
    hazards: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    estimated_crew_hours: float | None = None
    requires_specialist: bool = False
    image_quality_note: str | None = None
    confidence: float
    images: list[ImageMetadataOut] = Field(default_factory=list)
    location_check: dict[str, Any] = Field(
        default_factory=dict,
        description="Whether photo GPS agrees with the reported location.",
    )


class TriagePreviewRequest(InputModel):
    """Dry-run triage - lets staff see the routing before a report is filed."""

    title: str | None = Field(default=None, max_length=255)
    description: str = Field(min_length=10, max_length=8000)
    address: str | None = Field(default=None, max_length=500)


class TriagePreviewResponse(APIModel):
    category_slug: str
    confidence: float
    priority: str
    severity: str
    title: str
    summary: str
    suggested_department_slug: str | None = None
    is_emergency: bool
    requires_field_visit: bool
    tags: list[str] = Field(default_factory=list)
    reasoning: str
    missing_information: list[str] = Field(default_factory=list)
    would_be_applied: bool = Field(
        description="False when confidence is below the tenant's threshold."
    )


class TranslateRequest(InputModel):
    text: str = Field(min_length=1, max_length=8000)
    target_language: str = Field(min_length=2, max_length=8)
    source_language: str | None = Field(default=None, max_length=8)


class TranslateResponse(APIModel):
    detected_language: str
    translated_text: str
    is_translation_needed: bool


class DocumentUploadRequest(InputModel):
    title: str = Field(min_length=2, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    document_type: DocumentType = DocumentType.OTHER
    visibility: Visibility = Visibility.INTERNAL
    department_id: uuid.UUID | None = None
    reference_number: str | None = Field(default=None, max_length=120)
    issued_on: date | None = None
    effective_from: date | None = None
    expires_on: date | None = None
    language: str = Field(default="en", max_length=8)
    tags: list[str] = Field(default_factory=list, max_length=20)


class DocumentOut(APIModel):
    id: uuid.UUID
    title: str
    description: str | None = None
    document_type: DocumentType
    status: DocumentStatus
    visibility: Visibility
    department_id: uuid.UUID | None = None
    reference_number: str | None = None
    issued_on: date | None = None
    effective_from: date | None = None
    expires_on: date | None = None
    language: str
    tags: list[str]
    filename: str | None = None
    size_bytes: int
    page_count: int | None = None
    chunk_count: int
    token_estimate: int
    indexed_at: datetime | None = None
    index_error: str | None = None
    embedding_model: str | None = None
    created_at: datetime


class DocumentSearchResult(APIModel):
    document_id: uuid.UUID
    title: str
    page: int | None = None
    section: str | None = None
    excerpt: str
    score: float


class AIUsageOut(APIModel):
    period_days: int
    total_calls: int
    total_tokens: int
    estimated_cost_usd: float
    by_capability: list[dict[str, Any]] = Field(default_factory=list)


class BriefingRequest(InputModel):
    days: int = Field(default=7, ge=1, le=365)
    audience: str = Field(default="administrator", max_length=64)


class BriefingSectionOut(APIModel):
    heading: str
    body: str


class BriefingResponse(APIModel):
    headline: str
    key_points: list[str] = Field(default_factory=list)
    sections: list[BriefingSectionOut] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    period_label: str
    markdown: str = Field(description="The same briefing rendered for email or print.")
