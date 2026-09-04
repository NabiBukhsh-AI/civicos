"""Knowledge-base tests: ingestion, retrieval, permissions and the offline path."""

from __future__ import annotations

import pytest

from civicos.ai.assistant import ask
from civicos.ai.rag.ingest import ingest_document
from civicos.ai.rag.retriever import audience_for_role, retrieve
from civicos.core.context import Actor, set_actor
from civicos.domain.enums import DocumentStatus, DocumentType, Visibility
from civicos.domain.knowledge import Document

BYLAW = """SECTION 1 TRADE LICENCES

A trade licence is required for any commercial premises within the municipal
limits. The application must include a copy of the applicant identity document,
proof of the premises and a recent photograph. The prescribed fee is payable at
the time of application. Processing takes fourteen working days.

SECTION 2 RENEWAL

A licence is valid for one year from the date of issue and must be renewed
before it expires. A late renewal attracts a surcharge of ten per cent.

SECTION 3 WASTE COLLECTION

Household waste is collected daily. Commercial premises must arrange their own
collection through a licensed contractor and retain the receipts for inspection.
"""

INTERNAL_SOP = """STANDARD OPERATING PROCEDURE

Officers must verify the applicant identity document against the register
before approving any licence. Escalate discrepancies to the department head.
Do not disclose the verification method to applicants.
"""


@pytest.fixture(autouse=True)
def _system_actor() -> None:
    set_actor(Actor(kind="system", display_name="tests", is_superadmin=True))


async def _index(session, tenant, title: str, body: str, visibility: Visibility) -> Document:
    document = Document(
        tenant_id=tenant.id,
        title=title,
        document_type=DocumentType.BYLAW,
        visibility=visibility,
        filename=f"{title}.txt",
        content_type="text/plain",
    )
    session.add(document)
    await session.flush()
    await ingest_document(session, document, body.encode("utf-8"))
    await session.commit()
    return document


class TestIngestion:
    async def test_document_is_chunked_and_indexed(self, session, tenant) -> None:
        document = await _index(session, tenant, "Trade Licence Bylaw", BYLAW, Visibility.PUBLIC)

        assert document.status is DocumentStatus.INDEXED
        assert document.chunk_count > 0
        assert document.indexed_at is not None
        assert document.embedding_model

    async def test_unreadable_file_fails_with_a_useful_message(self, session, tenant) -> None:
        """A scanned PDF with no text layer is the most common upload problem."""
        document = Document(
            tenant_id=tenant.id,
            title="Empty",
            document_type=DocumentType.OTHER,
            filename="empty.txt",
            content_type="text/plain",
        )
        session.add(document)
        await session.flush()

        result = await ingest_document(session, document, b"   \n  ")

        assert not result.succeeded
        assert document.status is DocumentStatus.FAILED
        assert "OCR" in (document.index_error or "")

    async def test_reindexing_replaces_rather_than_appends(self, session, tenant) -> None:
        document = await _index(session, tenant, "Bylaw", BYLAW, Visibility.PUBLIC)
        first_count = document.chunk_count

        await ingest_document(session, document, BYLAW.encode("utf-8"))
        await session.commit()

        assert document.chunk_count == first_count, "re-indexing must be idempotent"


class TestRetrieval:
    async def test_relevant_passage_is_found(self, session, tenant) -> None:
        await _index(session, tenant, "Trade Licence Bylaw", BYLAW, Visibility.PUBLIC)

        result = await retrieve(session, tenant.id, "what documents are needed for a trade licence")

        assert not result.is_empty
        assert "identity document" in result.passages[0].chunk.content.lower()

    async def test_public_audience_cannot_see_internal_documents(self, session, tenant) -> None:
        await _index(session, tenant, "Officer SOP", INTERNAL_SOP, Visibility.INTERNAL)

        public = await retrieve(
            session, tenant.id, "verify the applicant identity document", audience=Visibility.PUBLIC
        )
        internal = await retrieve(
            session,
            tenant.id,
            "verify the applicant identity document",
            audience=Visibility.INTERNAL,
        )

        assert public.is_empty, "an internal SOP must never reach a citizen answer"
        assert not internal.is_empty

    async def test_audience_is_derived_from_role(self) -> None:
        assert audience_for_role("citizen", is_staff=False) is Visibility.PUBLIC
        assert audience_for_role("supervisor", is_staff=True) is Visibility.INTERNAL
        assert audience_for_role("tenant_admin", is_staff=True) is Visibility.RESTRICTED

    async def test_retrieval_is_tenant_scoped(self, session, tenant) -> None:
        from civicos.db.seed import seed_demo_tenant

        await _index(session, tenant, "Trade Licence Bylaw", BYLAW, Visibility.PUBLIC)
        other = await seed_demo_tenant(
            session,
            slug="elsewhere",
            name="Elsewhere",
            admin_email="admin@elsewhere.example",
            admin_password="ElsewherePass123!",
        )
        await session.commit()

        result = await retrieve(session, other.id, "trade licence documents")
        assert result.is_empty, "one municipality must not retrieve another's corpus"


class TestAssistant:
    async def test_answer_quotes_the_source_when_ai_is_disabled(self, session, tenant) -> None:
        """Offline, the assistant still hands back the municipality's own words."""
        await _index(session, tenant, "Trade Licence Bylaw", BYLAW, Visibility.PUBLIC)

        reply = await ask(
            session,
            tenant.id,
            "What documents do I need for a trade licence?",
            persist=False,
        )

        assert reply.grounded
        assert reply.citations, "an answer drawn from a document must cite it"
        assert "identity document" in reply.answer.lower()
        assert reply.citations[0]["title"] == "Trade Licence Bylaw"

    async def test_admits_ignorance_on_an_empty_corpus(self, session, tenant) -> None:
        reply = await ask(session, tenant.id, "What is the fee for a dog licence?", persist=False)

        assert not reply.grounded
        assert reply.escalate_to_human
        assert reply.citations == []
        assert "could not find" in reply.answer.lower()

    async def test_citations_never_reference_unretrieved_documents(self, session, tenant) -> None:
        """A fabricated source published to a resident is the worst failure here."""
        await _index(session, tenant, "Trade Licence Bylaw", BYLAW, Visibility.PUBLIC)

        reply = await ask(session, tenant.id, "trade licence requirements", persist=False)

        retrieved_titles = {"Trade Licence Bylaw"}
        assert all(citation["title"] in retrieved_titles for citation in reply.citations)

    async def test_conversation_persists_the_exchange(self, session, tenant) -> None:
        from civicos.ai.assistant import get_or_create_conversation

        await _index(session, tenant, "Trade Licence Bylaw", BYLAW, Visibility.PUBLIC)
        conversation = await get_or_create_conversation(
            session, tenant.id, session_key="anon-session-1"
        )

        await ask(
            session,
            tenant.id,
            "What documents do I need for a trade licence?",
            conversation=conversation,
        )
        await session.commit()

        assert conversation.message_count == 2, "the question and the answer are both kept"
        assert conversation.title
