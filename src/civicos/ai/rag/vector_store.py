"""Vector search over document chunks.

Two backends behind one interface:

* **pgvector** - the ANN index does the work in the database. Used when the
  connection is PostgreSQL and the extension is installed.
* **numpy** - candidates are pre-filtered in SQL (tenant, visibility, optional
  keyword match) and scored in Python. Correct everywhere, fast enough for the
  corpus size a single municipality actually has (thousands of chunks, not
  millions), and it is what lets the whole platform run on SQLite.

Both return the same rows, so the retriever above never branches on backend.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.core.config import get_settings
from civicos.core.text import cosine_similarity, tokenize
from civicos.domain.enums import DocumentStatus, Visibility
from civicos.domain.knowledge import Document, DocumentChunk

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class ScoredChunk:
    chunk: DocumentChunk
    document: Document
    score: float
    lexical_score: float = 0.0

    def to_citation(self) -> dict[str, Any]:
        return {
            "document_id": str(self.document.id),
            "title": self.document.title,
            "page": self.chunk.page_number,
            "section": self.chunk.section_title,
            "score": round(self.score, 4),
        }

    def to_passage(self) -> dict[str, Any]:
        return {
            "document_id": str(self.document.id),
            "title": self.document.title,
            "page": self.chunk.page_number,
            "content": self.chunk.content,
        }


class VectorStore(ABC):
    """Similarity search over a tenant's indexed chunks."""

    @abstractmethod
    async def search(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        embedding: Sequence[float],
        *,
        limit: int,
        visibilities: set[Visibility],
        document_ids: list[uuid.UUID] | None = None,
        keywords: list[str] | None = None,
    ) -> list[ScoredChunk]: ...


def _base_query(
    tenant_id: uuid.UUID,
    visibilities: set[Visibility],
    document_ids: list[uuid.UUID] | None,
) -> Select[Any]:
    query = (
        select(DocumentChunk, Document)
        .join(Document, DocumentChunk.document_id == Document.id)
        .where(
            DocumentChunk.tenant_id == tenant_id,
            DocumentChunk.visibility.in_(list(visibilities)),
            Document.status == DocumentStatus.INDEXED,
            Document.deleted_at.is_(None),
        )
    )
    if document_ids:
        query = query.where(Document.id.in_(document_ids))
    return query


class NumpyVectorStore(VectorStore):
    """Portable backend: SQL pre-filter, in-process cosine scoring."""

    #: Hard ceiling on rows pulled into memory for one query.
    MAX_CANDIDATES = 4000

    async def search(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        embedding: Sequence[float],
        *,
        limit: int,
        visibilities: set[Visibility],
        document_ids: list[uuid.UUID] | None = None,
        keywords: list[str] | None = None,
    ) -> list[ScoredChunk]:
        query = _base_query(tenant_id, visibilities, document_ids)

        # Narrow the candidate set with a cheap lexical filter when we can, so
        # a large corpus does not have to be scored in full.
        if keywords:
            clauses = [
                DocumentChunk.search_text.contains(keyword.lower())
                for keyword in keywords[:8]
                if len(keyword) > 2
            ]
            if clauses:
                keyword_rows = (
                    await session.execute(query.where(or_(*clauses)).limit(self.MAX_CANDIDATES))
                ).all()
                if len(keyword_rows) >= limit:
                    return self._score(keyword_rows, embedding, keywords, limit)

        rows = (await session.execute(query.limit(self.MAX_CANDIDATES))).all()
        return self._score(rows, embedding, keywords, limit)

    def _score(
        self,
        rows: Sequence[Any],
        embedding: Sequence[float],
        keywords: list[str] | None,
        limit: int,
    ) -> list[ScoredChunk]:
        settings = get_settings()
        keyword_set = {k.lower() for k in (keywords or []) if len(k) > 2}
        weight = settings.rag.keyword_weight
        query_vector = list(embedding)

        scored: list[ScoredChunk] = []
        for chunk, document in rows:
            semantic = cosine_similarity(query_vector, chunk.embedding) if chunk.embedding else 0.0
            lexical = _lexical_overlap(chunk.search_text or chunk.content, keyword_set)
            # Hybrid: semantic similarity carries the meaning, lexical overlap
            # rescues exact terms (a scheme number, a section reference) that
            # embeddings routinely blur.
            combined = (1 - weight) * semantic + weight * lexical
            scored.append(
                ScoredChunk(chunk=chunk, document=document, score=combined, lexical_score=lexical)
            )

        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:limit]


class PgVectorStore(VectorStore):
    """PostgreSQL backend using pgvector's cosine distance operator."""

    async def search(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        embedding: Sequence[float],
        *,
        limit: int,
        visibilities: set[Visibility],
        document_ids: list[uuid.UUID] | None = None,
        keywords: list[str] | None = None,
    ) -> list[ScoredChunk]:
        distance = DocumentChunk.embedding.cosine_distance(list(embedding))  # type: ignore[attr-defined]
        query = (
            _base_query(tenant_id, visibilities, document_ids)
            .where(DocumentChunk.embedding.is_not(None))
            .order_by(distance)
            .limit(limit)
        )
        if keywords:
            clauses = [
                DocumentChunk.search_text.contains(keyword.lower())
                for keyword in keywords[:8]
                if len(keyword) > 2
            ]
            if clauses:
                # Prefer keyword-matching chunks without excluding the rest.
                query = query.order_by(and_(*[or_(*clauses)]).desc(), distance)

        rows = (await session.execute(query)).all()
        keyword_set = {k.lower() for k in (keywords or []) if len(k) > 2}
        results: list[ScoredChunk] = []
        for chunk, document in rows:
            semantic = (
                cosine_similarity(list(embedding), chunk.embedding) if chunk.embedding else 0.0
            )
            results.append(
                ScoredChunk(
                    chunk=chunk,
                    document=document,
                    score=semantic,
                    lexical_score=_lexical_overlap(chunk.search_text or chunk.content, keyword_set),
                )
            )
        return results


def _lexical_overlap(text: str, keywords: set[str]) -> float:
    if not keywords:
        return 0.0
    tokens = set(tokenize(text))
    if not tokens:
        return 0.0
    return len(keywords & tokens) / len(keywords)


_store: VectorStore | None = None


async def get_vector_store(session: AsyncSession | None = None) -> VectorStore:
    """Pick a backend once, then reuse it for the process lifetime."""
    global _store
    if _store is not None:
        return _store

    settings = get_settings()
    backend = settings.rag.vector_backend

    if backend == "pgvector":
        _store = PgVectorStore()
    elif backend == "numpy":
        _store = NumpyVectorStore()
    else:  # auto
        from civicos.db.session import has_pgvector

        _store = PgVectorStore() if await has_pgvector() else NumpyVectorStore()

    logger.info("vector_store_selected", backend=type(_store).__name__)
    return _store


def reset_vector_store() -> None:
    global _store
    _store = None
