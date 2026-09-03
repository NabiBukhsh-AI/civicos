"""Structured-output contracts.

Every AI capability declares a Pydantic model here and hands its JSON Schema to
the provider. That gives three things a free-text prompt cannot: the response
is machine-checkable, the same shape is enforced across vendors, and the schema
doubles as documentation of exactly what the model is allowed to decide.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CategorySlug = str
PriorityLiteral = Literal["low", "normal", "high", "urgent", "emergency"]
SeverityLiteral = Literal["minor", "moderate", "major", "critical"]
SentimentLiteral = Literal["positive", "neutral", "negative", "mixed"]
ConditionLiteral = Literal["excellent", "good", "fair", "poor", "critical"]


class StrictModel(BaseModel):
    """Base for schema-enforced outputs (no extra keys, no surprises)."""

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def response_schema(cls) -> dict[str, Any]:
        """JSON Schema in the strict dialect providers expect."""
        return _strictify(cls.model_json_schema())


class TriageResult(StrictModel):
    """What the model proposes for an incoming report.

    Advisory only. The service layer records it in the ``ai_*`` columns and
    applies it as a *suggestion*; a human retains the final say, and low
    confidence routes the report to a manual triage queue.
    """

    category_slug: CategorySlug = Field(
        description="Slug of the best-matching category from the supplied taxonomy."
    )
    confidence: float = Field(ge=0.0, le=1.0, description="0-1 confidence in the category.")
    priority: PriorityLiteral = Field(description="Operational urgency.")
    severity: SeverityLiteral = Field(description="Physical seriousness of the condition.")
    title: str = Field(max_length=120, description="Short neutral title for the work queue.")
    summary: str = Field(
        max_length=600, description="Two-sentence factual summary for a supervisor."
    )
    suggested_department_slug: str | None = Field(
        default=None, description="Department slug that should own this, if clear."
    )
    is_emergency: bool = Field(description="True only for immediate risk to life or property.")
    requires_field_visit: bool = Field(description="Whether a crew must physically attend.")
    tags: list[str] = Field(default_factory=list, description="Up to five lowercase keywords.")
    reasoning: str = Field(
        max_length=400, description="Why this classification - shown to the supervisor."
    )
    missing_information: list[str] = Field(
        default_factory=list,
        description="What the reporter should be asked for, if anything.",
    )


class VisionFinding(StrictModel):
    """One issue spotted in one image."""

    label: str = Field(max_length=120)
    category_slug: CategorySlug
    severity: SeverityLiteral
    confidence: float = Field(ge=0.0, le=1.0)
    description: str = Field(max_length=400)


class VisionReport(StrictModel):
    """Structured reading of one or more site photographs."""

    summary: str = Field(max_length=800, description="What the images collectively show.")
    observations: list[str] = Field(
        default_factory=list, description="Concrete, checkable observations."
    )
    findings: list[VisionFinding] = Field(default_factory=list)
    primary_category_slug: CategorySlug
    severity: SeverityLiteral
    condition: ConditionLiteral = Field(description="Overall condition of the asset or site.")
    hazards: list[str] = Field(
        default_factory=list, description="Immediate safety hazards visible."
    )
    recommended_actions: list[str] = Field(default_factory=list)
    estimated_crew_hours: float | None = Field(
        default=None, ge=0.0, description="Rough labour estimate, if inferable."
    )
    requires_specialist: bool = Field(default=False)
    image_quality_note: str | None = Field(
        default=None,
        description="Set when blur, darkness or framing limits what can be concluded.",
    )
    confidence: float = Field(ge=0.0, le=1.0)


class DuplicateAssessment(StrictModel):
    """Adjudication between a new report and a nearby candidate."""

    is_duplicate: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=300)


class AnswerCitation(StrictModel):
    document_id: str
    title: str
    page: int | None = None
    quote: str = Field(max_length=400)


class GroundedAnswer(StrictModel):
    """A knowledge-base answer that must show its sources.

    ``answered_from_context`` is the honesty switch: when the retrieved
    passages do not support an answer the model says so instead of improvising
    municipal policy, which is the failure mode that matters most here.
    """

    answer: str = Field(max_length=4000)
    answered_from_context: bool = Field(
        description="False when the supplied documents do not contain the answer."
    )
    citations: list[AnswerCitation] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    follow_up_questions: list[str] = Field(default_factory=list)
    escalate_to_human: bool = Field(
        default=False, description="True when the resident needs a person, not a bot."
    )


class TranslationResult(StrictModel):
    detected_language: str = Field(max_length=8)
    translated_text: str
    is_translation_needed: bool


class ModerationResult(StrictModel):
    """Pre-publication check on citizen-authored text."""

    is_acceptable: bool
    contains_personal_data: bool
    is_abusive: bool
    is_spam: bool
    is_out_of_scope: bool = Field(
        description="True when the text is not about a municipal matter at all."
    )
    redacted_text: str = Field(description="Input with any personal data removed.")
    reason: str | None = Field(default=None, max_length=200)


class SentimentAnalysis(StrictModel):
    sentiment: SentimentLiteral
    score: float = Field(ge=-1.0, le=1.0)
    themes: list[str] = Field(default_factory=list, max_length=8)
    actionable_suggestion: str | None = Field(default=None, max_length=300)


class BriefingSection(StrictModel):
    heading: str = Field(max_length=120)
    body: str = Field(max_length=1200)


class ExecutiveBriefing(StrictModel):
    """The daily/weekly readout for a mayor, administrator or council."""

    headline: str = Field(max_length=200)
    key_points: list[str] = Field(default_factory=list, max_length=8)
    sections: list[BriefingSection] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list, max_length=6)
    recommended_actions: list[str] = Field(default_factory=list, max_length=6)
    period_label: str = Field(max_length=80)


class ApplicationReview(StrictModel):
    """Completeness check on a permit / licence application."""

    is_complete: bool
    missing_documents: list[str] = Field(default_factory=list)
    inconsistencies: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    recommendation: Literal["approve", "request_info", "inspect", "reject", "manual_review"]
    reasoning: str = Field(max_length=600)


class RoutingSuggestion(StrictModel):
    """Which crew should take a job, and when."""

    crew_code: str | None = None
    reasoning: str = Field(max_length=300)
    suggested_priority: PriorityLiteral
    estimated_hours: float | None = Field(default=None, ge=0.0)


def _strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$defs`` and force the strict-mode invariants.

    Providers that enforce schemas require ``additionalProperties: false`` and
    every property listed in ``required``; Pydantic omits optional fields from
    ``required``, so we add them back with nullable types.
    """
    defs = schema.pop("$defs", {})
    resolved = _resolve_refs(schema, defs)
    return _apply_strict(resolved)


def _resolve_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"].rsplit("/", 1)[-1]
            target = defs.get(ref, {})
            merged = {**_resolve_refs(target, defs)}
            merged.update({k: v for k, v in node.items() if k != "$ref"})
            return merged
        return {key: _resolve_refs(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(item, defs) for item in node]
    return node


def _apply_strict(node: Any) -> Any:
    if isinstance(node, dict):
        node = {key: _apply_strict(value) for key, value in node.items()}
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        # `anyOf: [{...}, {"type": "null"}]` is how Pydantic renders optionals;
        # keep it, since JSON Schema unions are widely supported.
        node.pop("default", None)
        return node
    if isinstance(node, list):
        return [_apply_strict(item) for item in node]
    return node
