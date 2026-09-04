"""Knowledge base: documents, their vector chunks, and assistant conversations.

This is the direct descendant of the original PDF chatbot, rebuilt as a
tenant-scoped, permission-aware, citation-carrying corpus rather than a single
global FAISS index on disk.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from civicos.core.config import get_settings
from civicos.db.base import (
    Base,
    MetadataMixin,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from civicos.db.types import (
    MutableJSONDict,
    MutableJSONList,
    StringEnum,
    UTCDateTime,
    Vector,
)
from civicos.domain.enums import (
    AICapability,
    ConversationRole,
    DocumentStatus,
    DocumentType,
    Visibility,
)

_EMBEDDING_DIMENSIONS = get_settings().ai.embedding_dimensions


class Document(
    UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, MetadataMixin, Base
):
    """A source document in the municipal corpus.

    ``visibility`` is enforced at retrieval time, so an internal SOP can sit in
    the same index as a public bylaw without ever leaking into a citizen-facing
    answer.
    """

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_tenant_status", "tenant_id", "status"),
        Index("ix_documents_tenant_type", "tenant_id", "document_type"),
        Index("ix_documents_tenant_visibility", "tenant_id", "visibility"),
        UniqueConstraint("tenant_id", "checksum", name="uq_documents_tenant_checksum"),
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    document_type: Mapped[DocumentType] = mapped_column(
        StringEnum(DocumentType), default=DocumentType.OTHER, nullable=False
    )
    status: Mapped[DocumentStatus] = mapped_column(
        StringEnum(DocumentStatus), default=DocumentStatus.UPLOADED, nullable=False
    )
    visibility: Mapped[Visibility] = mapped_column(
        StringEnum(Visibility), default=Visibility.INTERNAL, nullable=False
    )

    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )
    reference_number: Mapped[str | None] = mapped_column(String(120))
    issued_on: Mapped[date | None] = mapped_column(Date)
    effective_from: Mapped[date | None] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date)
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    tags: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)

    filename: Mapped[str | None] = mapped_column(String(255))
    storage_key: Mapped[str | None] = mapped_column(String(500))
    source_url: Mapped[str | None] = mapped_column(String(500))
    content_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64))
    page_count: Mapped[int | None] = mapped_column(Integer)

    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    indexed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    index_error: Mapped[str | None] = mapped_column(Text)
    embedding_model: Mapped[str | None] = mapped_column(String(120))

    ai_summary: Mapped[str | None] = mapped_column(Text)
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_searchable(self) -> bool:
        return self.status is DocumentStatus.INDEXED and not self.is_deleted


class DocumentChunk(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """One retrievable passage with its embedding and citation anchors."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        Index("ix_document_chunks_document", "document_id", "chunk_index"),
        Index("ix_document_chunks_tenant_visibility", "tenant_id", "visibility"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    page_number: Mapped[int | None] = mapped_column(Integer)
    section_title: Mapped[str | None] = mapped_column(String(300))
    #: Denormalised from the parent so retrieval filters without a join.
    visibility: Mapped[Visibility] = mapped_column(
        StringEnum(Visibility), default=Visibility.INTERNAL, nullable=False
    )
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)

    embedding: Mapped[list[float] | None] = mapped_column(Vector(_EMBEDDING_DIMENSIONS))
    #: Lower-cased content used by the lexical half of hybrid search.
    search_text: Mapped[str | None] = mapped_column(Text)

    document: Mapped[Document] = relationship(back_populates="chunks")


class Conversation(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A threaded session with the assistant.

    Persisting conversations is what turns a stateless Q&A endpoint into an
    assistant that can follow up ("and what about the previous year?"), and it
    gives supervisors a reviewable record of what the AI told residents.
    """

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_tenant_user", "tenant_id", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    session_key: Mapped[str | None] = mapped_column(String(64), index=True)
    """Opaque key so an anonymous resident keeps context without an account."""

    title: Mapped[str | None] = mapped_column(String(255))
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    audience: Mapped[Visibility] = mapped_column(
        StringEnum(Visibility), default=Visibility.PUBLIC, nullable=False
    )
    """Decides which documents the retriever is allowed to see."""

    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    messages: Mapped[list[ConversationMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.created_at",
    )


class ConversationMessage(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """One turn, with the citations and tool calls that produced it."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("ix_conversation_messages_conversation", "conversation_id", "created_at"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[ConversationRole] = mapped_column(StringEnum(ConversationRole), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    #: ``[{"document_id": ..., "title": ..., "page": 4, "score": 0.81}]``
    citations: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)
    tool_calls: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)

    model: Mapped[str | None] = mapped_column(String(120))
    provider: Mapped[str | None] = mapped_column(String(32))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    #: Mean retrieval score - a cheap, honest proxy for "did we actually know this".
    grounding_score: Mapped[float | None] = mapped_column(Float)
    feedback: Mapped[int | None] = mapped_column(Integer)
    """-1 / 0 / +1 thumb from the user, used to tune retrieval."""

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class AIUsage(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Per-call AI ledger.

    Municipal budgets are line-itemed and audited; "what did the AI cost this
    month, per department" has to be answerable without guesswork.
    """

    __tablename__ = "ai_usage"
    __table_args__ = (
        Index("ix_ai_usage_tenant_created", "tenant_id", "created_at"),
        Index("ix_ai_usage_tenant_capability", "tenant_id", "capability"),
    )

    capability: Mapped[AICapability] = mapped_column(StringEnum(AICapability), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )

    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    image_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    entity_type: Mapped[str | None] = mapped_column(String(48))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    details: Mapped[dict[str, Any]] = mapped_column(MutableJSONDict, default=dict, nullable=False)
