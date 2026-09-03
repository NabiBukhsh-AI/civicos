"""Civic image analysis.

The direct successor to the original image-processing endpoint, upgraded from
"return a paragraph of prose" to a validated :class:`VisionReport` that the
workflow engine can act on: a category, a severity, hazards, recommended
actions and a crew-hours estimate.

Photographs are also the evidence base for verification, so EXIF handling
(:mod:`civicos.integrations.exif`) sits alongside this rather than inside it.
"""

from __future__ import annotations

from typing import Any

import structlog

from civicos.ai.prompts import VISION_SYSTEM, taxonomy_block
from civicos.ai.schemas import VisionReport
from civicos.ai.types import CompletionRequest, ImagePart, Message
from civicos.ai.usage import UsageContext, run_completion
from civicos.core.config import get_settings
from civicos.core.errors import PayloadTooLargeError, ValidationError

logger = structlog.get_logger(__name__)

SUPPORTED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


async def analyse_images(
    images: list[ImagePart],
    *,
    categories: list[dict[str, Any]] | None = None,
    context_note: str | None = None,
    asset_hint: str | None = None,
    usage: UsageContext | None = None,
) -> VisionReport:
    """Produce a structured report from one or more site photographs."""
    settings = get_settings()
    validate_images(images, settings.ai.max_images_per_request, settings.ai.max_image_bytes)

    instructions: list[str] = []
    if categories:
        instructions.append(taxonomy_block(categories))
    if asset_hint:
        instructions.append(f"The images are believed to show: {asset_hint}")
    if context_note:
        instructions.append(f"Operator context: {context_note}")
    instructions.append(
        f"{len(images)} image(s) follow. Assess them together as one site, and "
        "note explicitly if they appear to show different locations."
    )

    request = CompletionRequest(
        messages=[
            Message(role="user", content="\n\n".join(instructions), images=list(images))
        ],
        system=VISION_SYSTEM,
        response_schema=VisionReport.response_schema(),
        schema_name="vision_report",
        max_output_tokens=1400,
        temperature=0.1,
        context={"image_count": len(images)},
    )

    result = await run_completion(request, capability="vision", usage=usage)
    payload = result.parsed or {}

    try:
        return VisionReport.model_validate(payload)
    except Exception as exc:
        logger.warning("vision_validation_failed", error=str(exc))
        return _fallback_report(len(images), result.text)


def validate_images(images: list[ImagePart], max_count: int, max_bytes: int) -> None:
    if not images:
        raise ValidationError("At least one image is required.", code="no_images")
    if len(images) > max_count:
        raise ValidationError(
            f"At most {max_count} images can be analysed in one request.",
            code="too_many_images",
            details={"submitted": len(images), "maximum": max_count},
        )
    for image in images:
        if image.size_bytes > max_bytes:
            raise PayloadTooLargeError(
                f"Image '{image.label or 'upload'}' exceeds the "
                f"{max_bytes // (1024 * 1024)} MB limit.",
                details={"size_bytes": image.size_bytes, "maximum": max_bytes},
            )
        if image.media_type not in SUPPORTED_MEDIA_TYPES:
            raise ValidationError(
                f"Unsupported image type '{image.media_type}'.",
                code="unsupported_image_type",
                details={"supported": sorted(SUPPORTED_MEDIA_TYPES)},
            )


def _fallback_report(image_count: int, raw_text: str) -> VisionReport:
    """Never lose the upload just because the model output was malformed."""
    return VisionReport(
        summary=(
            raw_text.strip()[:800]
            or f"{image_count} image(s) received; automated analysis was unavailable."
        ),
        observations=[],
        findings=[],
        primary_category_slug="other",
        severity="moderate",
        condition="fair",
        hazards=[],
        recommended_actions=["Manual review required - automated analysis was inconclusive."],
        estimated_crew_hours=None,
        requires_specialist=False,
        image_quality_note=None,
        confidence=0.1,
    )


def compare_before_after(
    before: VisionReport, after: VisionReport
) -> dict[str, Any]:
    """Compare two reports to support work-order verification.

    A crew's "after" photo should show a better condition than the "before".
    This is a deterministic comparison, not a second model call - it is used to
    flag suspicious completions for a supervisor, never to auto-reject work.
    """
    order = ["critical", "poor", "fair", "good", "excellent"]
    try:
        improvement = order.index(after.condition) - order.index(before.condition)
    except ValueError:
        improvement = 0

    resolved_hazards = [h for h in before.hazards if h not in after.hazards]
    new_hazards = [h for h in after.hazards if h not in before.hazards]

    return {
        "condition_before": before.condition,
        "condition_after": after.condition,
        "improvement_steps": improvement,
        "improved": improvement > 0,
        "resolved_hazards": resolved_hazards,
        "new_hazards": new_hazards,
        "category_changed": before.primary_category_slug != after.primary_category_slug,
        "needs_supervisor_review": improvement <= 0 or bool(new_hazards),
    }
