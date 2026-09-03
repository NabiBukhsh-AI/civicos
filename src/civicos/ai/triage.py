"""AI intake triage.

Turns a free-text report into a routing decision: category, department,
priority, severity, a neutral title and a supervisor-facing summary.

The output is *advisory*. :mod:`civicos.services.issue_service` writes it to
the ``ai_*`` columns and applies it only when confidence clears the tenant's
threshold; below that the report lands in a manual triage queue. Keeping the
model's opinion and the municipality's decision in separate columns is what
makes the automation auditable.
"""

from __future__ import annotations

from typing import Any

import structlog

from civicos.ai.prompts import TRIAGE_SYSTEM, report_block, taxonomy_block
from civicos.ai.schemas import TriageResult
from civicos.ai.types import CompletionRequest, Message
from civicos.ai.usage import UsageContext, run_completion
from civicos.core.text import redact_pii, truncate

logger = structlog.get_logger(__name__)

#: Below this the routing is treated as a suggestion only.
DEFAULT_CONFIDENCE_FLOOR = 0.5


async def triage_report(
    *,
    title: str,
    description: str,
    categories: list[dict[str, Any]],
    location: str | None = None,
    channel: str | None = None,
    reported_at: str | None = None,
    usage: UsageContext | None = None,
) -> TriageResult:
    """Classify one report against the tenant's taxonomy.

    ``categories`` comes from the tenant's own ``issue_categories`` rows, so a
    town that renames or adds a category changes classifier behaviour without a
    deployment.
    """
    valid_slugs = {category["slug"] for category in categories} | {"other"}

    prompt = "\n\n".join(
        [
            taxonomy_block(categories),
            "Report to classify:",
            report_block(
                title=title,
                # The model does not need the reporter's phone number to route a pothole.
                description=redact_pii(truncate(description, 4000)),
                location=location,
                channel=channel,
                reported_at=reported_at,
            ),
        ]
    )

    request = CompletionRequest(
        messages=[Message(role="user", content=prompt)],
        system=TRIAGE_SYSTEM,
        response_schema=TriageResult.response_schema(),
        schema_name="triage_result",
        max_output_tokens=900,
        temperature=0.0,
        context={
            "categories": len(categories),
            # Used only by the offline provider, ignored by real ones.
            "classify_text": f"{title}\n{description}",
        },
    )

    result = await run_completion(request, capability="triage", usage=usage)
    payload = result.parsed or {}

    try:
        triage = TriageResult.model_validate(payload)
    except Exception as exc:
        logger.warning("triage_validation_failed", error=str(exc))
        return _fallback(title, description, valid_slugs)

    if triage.category_slug not in valid_slugs:
        # A hallucinated slug must not silently create a phantom category.
        logger.info("triage_unknown_slug", slug=triage.category_slug)
        triage = triage.model_copy(
            update={
                "category_slug": "other",
                "confidence": min(triage.confidence, 0.3),
                "reasoning": (
                    f"Model proposed unknown category '{triage.category_slug}'; "
                    "routed to general intake."
                ),
            }
        )
    return triage


def _fallback(title: str, description: str, valid_slugs: set[str]) -> TriageResult:
    """Rule-based triage used when the model output cannot be trusted."""
    from civicos.ai.providers.heuristic import classify  # noqa: PLC0415

    slug, priority, severity, matched = classify(f"{title} {description}")
    if slug not in valid_slugs:
        slug = "other"
    return TriageResult(
        category_slug=slug,
        confidence=0.3 if matched else 0.15,
        priority=priority,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        title=truncate(title, 120),
        summary=truncate(description, 600),
        suggested_department_slug=None,
        is_emergency=priority == "emergency",
        requires_field_visit=severity in {"major", "critical"},
        tags=[slug],
        reasoning="Rule-based fallback: the model response could not be validated.",
        missing_information=[],
    )


def is_confident(result: TriageResult, floor: float = DEFAULT_CONFIDENCE_FLOOR) -> bool:
    return result.confidence >= floor
