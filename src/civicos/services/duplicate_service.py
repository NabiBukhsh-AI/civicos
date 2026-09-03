"""Duplicate detection.

Duplicates are the dominant failure mode of any public reporting channel: one
burst water main produces forty reports, and a queue of forty makes the
department look forty times behind. Merging them turns noise into signal - the
merged count becomes evidence of how many households are affected.

The check runs in escalating cost order, and stops at the first confident
answer:

1. **Fingerprint** - byte-identical resubmission within a day (a double tap).
2. **Geo + lexical** - same category, within a radius, overlapping wording.
3. **Semantic** - embedding cosine similarity.
4. **Model adjudication** - only for genuinely borderline pairs.

Being wrong in the "merge" direction silently buries a resident's report, so
the thresholds are asymmetric: we merge only on strong evidence and otherwise
flag for a human.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Sequence

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.ai.prompts import DUPLICATE_SYSTEM, report_block
from civicos.ai.registry import is_ai_enabled
from civicos.ai.schemas import DuplicateAssessment
from civicos.ai.types import CompletionRequest, Message
from civicos.ai.usage import UsageContext, run_completion
from civicos.core.config import get_settings
from civicos.core.geo import Point, haversine_meters
from civicos.core.text import cosine_similarity, content_fingerprint, jaccard_similarity
from civicos.domain.issues import Issue
from civicos.repositories.issues import IssueRepository

logger = structlog.get_logger(__name__)

#: Cosine thresholds differ by embedding backend: model embeddings separate
#: near-duplicates far more sharply than the offline hashing embedder does.
MODEL_EMBEDDING_THRESHOLD = 0.85
OFFLINE_EMBEDDING_THRESHOLD = 0.55

#: Above this combined score we merge automatically; between the two we flag.
AUTO_MERGE_SCORE = 0.80
REVIEW_SCORE = 0.55


@dataclass(slots=True)
class DuplicateCandidate:
    issue: Issue
    distance_meters: float
    lexical_score: float
    semantic_score: float
    combined_score: float
    reason: str

    @property
    def should_merge(self) -> bool:
        return self.combined_score >= AUTO_MERGE_SCORE

    @property
    def needs_review(self) -> bool:
        return REVIEW_SCORE <= self.combined_score < AUTO_MERGE_SCORE


@dataclass(slots=True)
class DuplicateVerdict:
    candidates: list[DuplicateCandidate]
    exact_match: Issue | None = None

    @property
    def best(self) -> DuplicateCandidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def auto_merge_target(self) -> Issue | None:
        if self.exact_match is not None:
            return self.exact_match
        best = self.best
        return best.issue if best and best.should_merge else None


def fingerprint_for(title: str, description: str, latitude: float | None, longitude: float | None) -> str:
    """Hash of the report's substance, rounded so tiny GPS jitter still matches."""
    coordinates = (
        f"{latitude:.4f},{longitude:.4f}" if latitude is not None and longitude is not None else ""
    )
    return content_fingerprint(title, description, coordinates)


async def find_duplicates(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    title: str,
    description: str,
    category_id: uuid.UUID | None,
    latitude: float | None,
    longitude: float | None,
    embedding: Sequence[float] | None = None,
    exclude_id: uuid.UUID | None = None,
    fingerprint: str | None = None,
    use_model: bool = True,
    usage: UsageContext | None = None,
) -> DuplicateVerdict:
    """Score existing issues against a new report."""
    settings = get_settings()
    repository = IssueRepository(session, tenant_id)

    if fingerprint:
        exact = await repository.find_by_fingerprint(fingerprint, within_hours=24)
        if exact is not None and exact.id != exclude_id:
            logger.info("duplicate_exact_fingerprint", existing=exact.reference)
            return DuplicateVerdict(candidates=[], exact_match=exact)

    if latitude is None or longitude is None:
        # Without a location, only near-identical text is defensible evidence.
        return DuplicateVerdict(candidates=[])

    point = Point(latitude, longitude)
    nearby = await repository.find_nearby(
        point,
        settings.geo.duplicate_radius_meters,
        within_hours=settings.geo.duplicate_time_window_hours,
        category_id=category_id,
        exclude_id=exclude_id,
        limit=25,
    )
    if not nearby:
        return DuplicateVerdict(candidates=[])

    semantic_threshold = (
        MODEL_EMBEDDING_THRESHOLD if is_ai_enabled() else OFFLINE_EMBEDDING_THRESHOLD
    )
    new_text = f"{title} {description}"
    candidates: list[DuplicateCandidate] = []

    for issue, distance in nearby:
        lexical = jaccard_similarity(new_text, f"{issue.title} {issue.description}")
        semantic = (
            cosine_similarity(embedding, issue.embedding)
            if embedding and issue.embedding
            else 0.0
        )
        combined = _combine(
            distance_meters=distance,
            radius=settings.geo.duplicate_radius_meters,
            lexical=lexical,
            semantic=semantic,
            semantic_threshold=semantic_threshold,
            same_category=category_id is not None and issue.category_id == category_id,
        )
        candidates.append(
            DuplicateCandidate(
                issue=issue,
                distance_meters=round(distance, 1),
                lexical_score=round(lexical, 3),
                semantic_score=round(semantic, 3),
                combined_score=round(combined, 3),
                reason=(
                    f"{distance:.0f} m away, lexical {lexical:.2f}, semantic {semantic:.2f}"
                ),
            )
        )

    candidates.sort(key=lambda candidate: candidate.combined_score, reverse=True)
    candidates = [c for c in candidates if c.combined_score >= 0.3][:5]

    # Only pay for a model call on the genuinely ambiguous top candidate.
    top = candidates[0] if candidates else None
    if use_model and top is not None and top.needs_review and is_ai_enabled():
        assessment = await _adjudicate(
            title=title, description=description, other=top.issue, usage=usage
        )
        if assessment is not None:
            adjusted = (top.combined_score + assessment.confidence) / 2
            top.combined_score = round(
                adjusted if assessment.is_duplicate else min(adjusted, REVIEW_SCORE - 0.01),
                3,
            )
            top.reason = f"{top.reason}; model: {assessment.reasoning}"
            candidates.sort(key=lambda candidate: candidate.combined_score, reverse=True)

    return DuplicateVerdict(candidates=candidates)


def _combine(
    *,
    distance_meters: float,
    radius: int,
    lexical: float,
    semantic: float,
    semantic_threshold: float,
    same_category: bool,
) -> float:
    """Blend proximity, wording and meaning into one score.

    Proximity is necessary but never sufficient - two unrelated problems at one
    junction must not merge - so text similarity carries most of the weight and
    distance acts as a multiplier that decays to zero at the search radius.
    """
    proximity = max(0.0, 1.0 - (distance_meters / max(radius, 1)))
    normalised_semantic = min(semantic / semantic_threshold, 1.0) if semantic > 0 else 0.0
    text_signal = max(lexical, normalised_semantic * 0.95)

    score = (0.62 * text_signal) + (0.28 * proximity)
    if same_category:
        score += 0.10
    return min(score, 1.0)


async def _adjudicate(
    *,
    title: str,
    description: str,
    other: Issue,
    usage: UsageContext | None,
) -> DuplicateAssessment | None:
    prompt = "\n\n".join(
        [
            "Report A (new):",
            report_block(title=title, description=description),
            "Report B (existing):",
            report_block(
                title=other.title,
                description=other.description,
                location=other.address,
                reported_at=other.created_at.isoformat() if other.created_at else None,
            ),
            "Are these the same physical problem at the same place?",
        ]
    )
    request = CompletionRequest(
        messages=[Message(role="user", content=prompt)],
        system=DUPLICATE_SYSTEM,
        response_schema=DuplicateAssessment.response_schema(),
        schema_name="duplicate_assessment",
        max_output_tokens=400,
        temperature=0.0,
    )
    try:
        result = await run_completion(
            request, capability="duplicate_check", usage=usage, allow_fallback=False
        )
        return DuplicateAssessment.model_validate(result.parsed or {})
    except Exception as exc:
        logger.debug("duplicate_adjudication_skipped", error=str(exc))
        return None
