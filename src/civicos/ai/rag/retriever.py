"""Retrieval: turn a question into grounded passages.

Three things distinguish this from a plain similarity search:

* **Permission-aware.** The caller's audience decides which visibility levels
  are searchable, so an internal SOP can never surface in a citizen answer.
* **Hybrid + over-fetch.** We fetch a multiple of ``top_k`` candidates and then
  re-rank, which materially improves recall on short, keyword-heavy municipal
  queries ("what is the trade licence fee for a tea stall").
* **Budgeted.** Passages are packed to a character budget so a long bylaw
  cannot crowd the answer out of the context window.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.ai.rag.vector_store import ScoredChunk, get_vector_store
from civicos.ai.usage import UsageContext, run_embedding
from civicos.core.config import get_settings
from civicos.core.text import tokenize
from civicos.domain.enums import Visibility

logger = structlog.get_logger(__name__)

#: Which visibility levels each audience may retrieve from.
AUDIENCE_VISIBILITY: dict[Visibility, set[Visibility]] = {
    Visibility.PUBLIC: {Visibility.PUBLIC},
    Visibility.INTERNAL: {Visibility.PUBLIC, Visibility.INTERNAL},
    Visibility.RESTRICTED: {Visibility.PUBLIC, Visibility.INTERNAL, Visibility.RESTRICTED},
}


@dataclass(slots=True)
class RetrievalResult:
    passages: list[ScoredChunk] = field(default_factory=list)
    query_tokens: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.passages

    @property
    def mean_score(self) -> float:
        if not self.passages:
            return 0.0
        return sum(p.score for p in self.passages) / len(self.passages)

    @property
    def top_score(self) -> float:
        return self.passages[0].score if self.passages else 0.0

    def as_prompt_passages(self) -> list[dict[str, Any]]:
        return [passage.to_passage() for passage in self.passages]

    def as_citations(self) -> list[dict[str, Any]]:
        return [passage.to_citation() for passage in self.passages]


async def retrieve(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    question: str,
    *,
    audience: Visibility = Visibility.PUBLIC,
    top_k: int | None = None,
    document_ids: list[uuid.UUID] | None = None,
    usage: UsageContext | None = None,
) -> RetrievalResult:
    """Retrieve the passages most likely to answer ``question``."""
    settings = get_settings()
    top_k = top_k or settings.rag.top_k
    if not question.strip():
        return RetrievalResult()

    embedding_result = await run_embedding([question], usage=usage)
    if not embedding_result.vectors:
        return RetrievalResult()

    keywords = tokenize(question)[:12]
    store = await get_vector_store(session)
    candidates = await store.search(
        session,
        tenant_id,
        embedding_result.vectors[0],
        limit=top_k * settings.rag.candidate_multiplier,
        visibilities=AUDIENCE_VISIBILITY.get(audience, {Visibility.PUBLIC}),
        document_ids=document_ids,
        keywords=keywords,
    )

    selected, truncated = _pack(
        [c for c in candidates if c.score >= settings.rag.min_similarity],
        top_k=top_k,
        char_budget=settings.rag.max_context_chars,
    )

    logger.debug(
        "retrieval_complete",
        candidates=len(candidates),
        selected=len(selected),
        top_score=round(selected[0].score, 3) if selected else 0.0,
    )
    return RetrievalResult(passages=selected, query_tokens=keywords, truncated=truncated)


def _pack(
    candidates: list[ScoredChunk], *, top_k: int, char_budget: int
) -> tuple[list[ScoredChunk], bool]:
    """Select passages under a character budget, avoiding near-duplicates.

    Municipal corpora repeat themselves heavily - the same clause appears in a
    bylaw, its amendment and the summary circular. Diversifying by document
    gives the model three angles instead of the same paragraph three times.
    """
    selected: list[ScoredChunk] = []
    used_chars = 0
    seen_documents: dict[str, int] = {}
    truncated = False

    for candidate in candidates:
        if len(selected) >= top_k:
            truncated = True
            break
        document_key = str(candidate.document.id)
        # At most two chunks from any one document until we have breadth.
        if seen_documents.get(document_key, 0) >= 2 and len(seen_documents) > 1:
            continue
        length = len(candidate.chunk.content)
        if used_chars + length > char_budget:
            truncated = True
            continue
        selected.append(candidate)
        used_chars += length
        seen_documents[document_key] = seen_documents.get(document_key, 0) + 1

    return selected, truncated


def audience_for_role(role: str | None, is_staff: bool) -> Visibility:
    """Map an actor onto the strongest visibility they may retrieve."""
    if not is_staff:
        return Visibility.PUBLIC
    if role in {"super_admin", "tenant_admin", "department_head"}:
        return Visibility.RESTRICTED
    return Visibility.INTERNAL
