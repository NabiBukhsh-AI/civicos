"""Document ingestion pipeline.

Upload -> parse -> chunk -> embed -> index, with the document row acting as the
state machine (``uploaded`` -> ``processing`` -> ``indexed`` / ``failed``).
Re-ingesting a document replaces its chunks atomically so a failed re-index can
never leave a half-updated corpus behind.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.ai.rag.chunking import chunk_document, estimate_tokens
from civicos.ai.usage import UsageContext, run_embedding
from civicos.core.clock import utcnow
from civicos.core.config import get_settings
from civicos.core.errors import NotFoundError
from civicos.domain.enums import DocumentStatus
from civicos.domain.knowledge import Document, DocumentChunk

logger = structlog.get_logger(__name__)

#: Embedding calls are batched; providers reject very large batches.
EMBED_BATCH_SIZE = 64


@dataclass(slots=True)
class IngestResult:
    document_id: uuid.UUID
    chunk_count: int
    token_estimate: int
    page_count: int | None
    status: DocumentStatus
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is DocumentStatus.INDEXED


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def ingest_document(
    session: AsyncSession,
    document: Document,
    data: bytes,
    *,
    usage: UsageContext | None = None,
) -> IngestResult:
    """Parse, chunk, embed and index one document.

    Failures are recorded on the document row rather than raised: a corrupt PDF
    in a bulk upload should mark that one file failed, not abort the batch.
    """
    settings = get_settings()
    document.status = DocumentStatus.PROCESSING
    document.index_error = None
    await session.flush()

    try:
        parsed, chunks = chunk_document(
            data,
            document.content_type or "application/octet-stream",
            document.filename or "",
            chunk_size=settings.rag.chunk_size,
            overlap=settings.rag.chunk_overlap,
        )
    except Exception as exc:
        logger.warning("document_parse_failed", document_id=str(document.id), error=str(exc))
        document.status = DocumentStatus.FAILED
        document.index_error = str(exc)[:1000]
        await session.flush()
        return IngestResult(document.id, 0, 0, None, DocumentStatus.FAILED, str(exc))

    if not chunks:
        document.status = DocumentStatus.FAILED
        document.index_error = (
            "No extractable text. The file may be a scanned image; run OCR before upload."
        )
        await session.flush()
        return IngestResult(
            document.id, 0, 0, parsed.page_count, DocumentStatus.FAILED, document.index_error
        )

    # Replace rather than append, so re-indexing is idempotent.
    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))

    embedding_model = "unknown"
    total_tokens = 0
    for batch_start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[batch_start : batch_start + EMBED_BATCH_SIZE]
        embeddings = await run_embedding([chunk.content for chunk in batch], usage=usage)
        embedding_model = embeddings.model
        for chunk, vector in zip(batch, embeddings.vectors, strict=False):
            total_tokens += chunk.token_estimate
            session.add(
                DocumentChunk(
                    tenant_id=document.tenant_id,
                    document_id=document.id,
                    chunk_index=chunk.index,
                    content=chunk.content,
                    token_estimate=chunk.token_estimate,
                    page_number=chunk.page_number,
                    section_title=chunk.section_title,
                    visibility=document.visibility,
                    language=document.language,
                    embedding=vector,
                    search_text=chunk.content.lower()[:8000],
                )
            )

    document.status = DocumentStatus.INDEXED
    document.chunk_count = len(chunks)
    document.token_estimate = total_tokens
    document.page_count = parsed.page_count
    document.indexed_at = utcnow()
    document.embedding_model = embedding_model
    if not document.checksum:
        document.checksum = checksum(data)
    await session.flush()

    logger.info(
        "document_indexed",
        document_id=str(document.id),
        chunks=len(chunks),
        tokens=total_tokens,
    )
    return IngestResult(
        document.id,
        len(chunks),
        total_tokens,
        parsed.page_count,
        DocumentStatus.INDEXED,
    )


async def reindex_document(
    session: AsyncSession,
    document_id: uuid.UUID,
    data: bytes,
    *,
    usage: UsageContext | None = None,
) -> IngestResult:
    document = await session.get(Document, document_id)
    if document is None:
        raise NotFoundError("Document not found.", code="document_not_found")
    return await ingest_document(session, document, data, usage=usage)


async def index_text(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    document: Document,
    text: str,
    *,
    usage: UsageContext | None = None,
) -> IngestResult:
    """Index text that did not arrive as a file (a pasted notice, a web page)."""
    from civicos.ai.rag.chunking import chunk_text

    chunks = chunk_text(text)
    if not chunks:
        document.status = DocumentStatus.FAILED
        document.index_error = "Empty document."
        await session.flush()
        return IngestResult(document.id, 0, 0, None, DocumentStatus.FAILED, "Empty document.")

    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
    embeddings = await run_embedding([chunk.content for chunk in chunks], usage=usage)
    for chunk, vector in zip(chunks, embeddings.vectors, strict=False):
        session.add(
            DocumentChunk(
                tenant_id=tenant_id,
                document_id=document.id,
                chunk_index=chunk.index,
                content=chunk.content,
                token_estimate=chunk.token_estimate,
                page_number=chunk.page_number,
                section_title=chunk.section_title,
                visibility=document.visibility,
                language=document.language,
                embedding=vector,
                search_text=chunk.content.lower()[:8000],
            )
        )

    document.status = DocumentStatus.INDEXED
    document.chunk_count = len(chunks)
    document.token_estimate = sum(c.token_estimate for c in chunks)
    document.indexed_at = utcnow()
    document.embedding_model = embeddings.model
    await session.flush()
    return IngestResult(
        document.id,
        len(chunks),
        document.token_estimate,
        None,
        DocumentStatus.INDEXED,
    )


async def find_by_checksum(
    session: AsyncSession, tenant_id: uuid.UUID, digest: str
) -> Document | None:
    """Detect a re-upload of a file the tenant already holds."""
    return await session.scalar(
        select(Document).where(
            Document.tenant_id == tenant_id,
            Document.checksum == digest,
            Document.deleted_at.is_(None),
        )
    )


def estimate_index_cost(text: str) -> int:
    return estimate_tokens(text)
