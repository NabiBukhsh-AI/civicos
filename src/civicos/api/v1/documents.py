"""Document corpus endpoints: upload, index, search, manage."""

from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from sqlalchemy import select

from civicos.ai.rag.ingest import checksum, find_by_checksum, ingest_document
from civicos.ai.rag.retriever import audience_for_role, retrieve
from civicos.ai.usage import UsageContext
from civicos.api.deps import (
    CurrentUserDep,
    OptionalActorDep,
    PageDep,
    SessionDep,
    TenantDep,
    require_permission,
)
from civicos.core.errors import ConflictError, NotFoundError, ValidationError
from civicos.core.pagination import Page as PageResult
from civicos.core.permissions import Resource, is_staff, perm
from civicos.core.text import truncate
from civicos.domain.enums import DocumentStatus, DocumentType, Visibility
from civicos.domain.knowledge import Document
from civicos.integrations.storage import get_storage, validate_upload, verify_declared_type
from civicos.schemas.ai import DocumentOut, DocumentSearchResult
from civicos.schemas.common import Message, Page

router = APIRouter(prefix="/documents", tags=["Knowledge Base"])

READ = perm(Resource.DOCUMENT, "read")
CREATE = perm(Resource.DOCUMENT, "create")


@router.post(
    "",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(CREATE))],
)
async def upload_document(
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
    file: Annotated[UploadFile, File(description="PDF, DOCX or plain text.")],
    title: Annotated[str, Form(min_length=2, max_length=300)],
    document_type: Annotated[DocumentType, Form()] = DocumentType.OTHER,
    visibility: Annotated[Visibility, Form()] = Visibility.INTERNAL,
    description: Annotated[str | None, Form(max_length=2000)] = None,
    department_id: Annotated[uuid.UUID | None, Form()] = None,
    reference_number: Annotated[str | None, Form(max_length=120)] = None,
    language: Annotated[str, Form(max_length=8)] = "en",
    tags: Annotated[str | None, Form(description="JSON array or comma-separated.")] = None,
    index_now: Annotated[bool, Form()] = True,
) -> DocumentOut:
    """Upload a document and index it for the assistant.

    Indexing is synchronous by default so the uploader learns immediately
    whether the file was readable - scanned PDFs with no text layer are the
    most common upload problem, and silence would be the wrong answer.
    """
    from civicos.core.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    data = await file.read()
    content_type = verify_declared_type(data, file.content_type or "")
    validate_upload(
        data,
        content_type,
        allowed_types=settings.storage.allowed_document_types,
        max_bytes=settings.storage.max_upload_bytes,
        label="document",
    )

    digest = checksum(data)
    if (existing := await find_by_checksum(session, tenant.id, digest)) is not None:
        raise ConflictError(
            f"This file is already in the library as '{existing.title}'.",
            code="document_duplicate",
            details={"document_id": str(existing.id)},
        )

    storage = get_storage()
    stored = await storage.save(
        data,
        tenant_id=tenant.id,
        filename=file.filename or "document",
        content_type=content_type,
        folder="documents",
    )

    document = Document(
        tenant_id=tenant.id,
        title=title,
        description=description,
        document_type=document_type,
        visibility=visibility,
        department_id=department_id,
        reference_number=reference_number,
        language=language,
        tags=_parse_tags(tags),
        filename=stored.filename,
        storage_key=stored.key,
        content_type=content_type,
        size_bytes=stored.size_bytes,
        checksum=digest,
        uploaded_by_id=user.id,
    )
    session.add(document)
    await session.flush()

    if index_now:
        await ingest_document(
            session,
            document,
            data,
            usage=UsageContext.from_request(
                session, tenant_id=tenant.id, entity_type="document", entity_id=document.id
            ),
        )
    await session.commit()
    return DocumentOut.model_validate(document)


@router.get("", response_model=Page[DocumentOut])
async def list_documents(
    session: SessionDep,
    tenant: TenantDep,
    page: PageDep,
    actor: OptionalActorDep,
    document_type: Annotated[DocumentType | None, Query()] = None,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> Page[DocumentOut]:
    """List documents the caller is permitted to see."""
    from sqlalchemy import func, or_  # noqa: PLC0415

    visibilities = _visible_levels(actor)
    statement = select(Document).where(
        Document.tenant_id == tenant.id,
        Document.deleted_at.is_(None),
        Document.visibility.in_(list(visibilities)),
    )
    if document_type:
        statement = statement.where(Document.document_type == document_type)
    if status_filter:
        statement = statement.where(Document.status == status_filter)
    if search:
        term = f"%{search.lower()}%"
        statement = statement.where(
            or_(
                func.lower(Document.title).like(term),
                func.lower(func.coalesce(Document.description, "")).like(term),
                func.lower(func.coalesce(Document.reference_number, "")).like(term),
            )
        )

    total = int(
        await session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    )
    rows = (
        await session.scalars(
            statement.order_by(Document.created_at.desc())
            .offset(page.offset)
            .limit(page.limit)
        )
    ).all()
    result = PageResult.build([DocumentOut.model_validate(row) for row in rows], total, page)
    return Page[DocumentOut].model_validate(result.model_dump())


@router.get("/search", response_model=list[DocumentSearchResult])
async def search_documents(
    session: SessionDep,
    tenant: TenantDep,
    actor: OptionalActorDep,
    q: Annotated[str, Query(min_length=2, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=25)] = 8,
) -> list[DocumentSearchResult]:
    """Semantic search over the corpus, returning passages rather than files."""
    audience = audience_for_role(actor.role, is_staff(actor.role or ""))
    retrieval = await retrieve(
        session,
        tenant.id,
        q,
        audience=audience,
        top_k=limit,
        usage=UsageContext.from_request(session, tenant_id=tenant.id),
    )
    await session.commit()
    return [
        DocumentSearchResult(
            document_id=passage.document.id,
            title=passage.document.title,
            page=passage.chunk.page_number,
            section=passage.chunk.section_title,
            excerpt=truncate(passage.chunk.content, 400),
            score=round(passage.score, 4),
        )
        for passage in retrieval.passages
    ]


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: uuid.UUID,
    session: SessionDep,
    tenant: TenantDep,
    actor: OptionalActorDep,
) -> DocumentOut:
    document = await _load(session, tenant.id, document_id)
    if document.visibility not in _visible_levels(actor):
        raise NotFoundError("Document not found.", code="document_not_found")
    return DocumentOut.model_validate(document)


@router.post(
    "/{document_id}/reindex",
    response_model=DocumentOut,
    dependencies=[Depends(require_permission(CREATE))],
)
async def reindex(
    document_id: uuid.UUID, session: SessionDep, tenant: TenantDep
) -> DocumentOut:
    """Re-run extraction and embedding, e.g. after changing the embedding model."""
    document = await _load(session, tenant.id, document_id)
    if not document.storage_key:
        raise ValidationError(
            "This document has no stored file to re-index.", code="no_stored_file"
        )
    data = await get_storage().load(document.storage_key)
    await ingest_document(
        session,
        document,
        data,
        usage=UsageContext.from_request(
            session, tenant_id=tenant.id, entity_type="document", entity_id=document.id
        ),
    )
    await session.commit()
    return DocumentOut.model_validate(document)


@router.delete(
    "/{document_id}",
    response_model=Message,
    dependencies=[Depends(require_permission(perm(Resource.DOCUMENT, "delete")))],
)
async def archive_document(
    document_id: uuid.UUID, session: SessionDep, tenant: TenantDep
) -> Message:
    """Archive a document and remove it from the search index.

    The row and the stored file are retained - municipal records are not
    deleted, they are withdrawn from circulation.
    """
    from sqlalchemy import delete  # noqa: PLC0415

    from civicos.core.clock import utcnow  # noqa: PLC0415
    from civicos.domain.knowledge import DocumentChunk  # noqa: PLC0415

    document = await _load(session, tenant.id, document_id)
    document.status = DocumentStatus.ARCHIVED
    document.deleted_at = utcnow()
    await session.execute(
        delete(DocumentChunk).where(DocumentChunk.document_id == document.id)
    )
    await session.commit()
    return Message(
        message=f"'{document.title}' archived.",
        detail="It is no longer searchable, but the record and file are retained.",
    )


# ---------------------------------------------------------------- helpers ----


async def _load(session, tenant_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    document = await session.get(Document, document_id)
    if document is None or document.tenant_id != tenant_id or document.is_deleted:
        raise NotFoundError("Document not found.", code="document_not_found")
    return document


def _visible_levels(actor) -> set[Visibility]:
    from civicos.ai.rag.retriever import AUDIENCE_VISIBILITY  # noqa: PLC0415

    audience = audience_for_role(actor.role, is_staff(actor.role or ""))
    return AUDIENCE_VISIBILITY.get(audience, {Visibility.PUBLIC})


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            return [str(item) for item in parsed][:20]
        except json.JSONDecodeError:
            pass
    return [part.strip() for part in raw.split(",") if part.strip()][:20]
