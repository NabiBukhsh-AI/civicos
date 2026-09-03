"""The municipal assistant: grounded Q&A over the tenant's own documents.

This is the descendant of the original PDF chatbot. What changed:

* retrieval is tenant-scoped and permission-aware, not one global index;
* answers must cite the passages they rest on, and the model is required to
  say when the corpus does not answer the question;
* conversations persist, so follow-up questions work and supervisors can audit
  what residents were told;
* a low grounding score is surfaced rather than hidden, because "I could not
  find that" is the correct answer far more often than a confident guess.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.ai.prompts import ASSISTANT_SYSTEM, context_block
from civicos.ai.rag.retriever import RetrievalResult, retrieve
from civicos.ai.schemas import GroundedAnswer
from civicos.ai.types import CompletionRequest, Message
from civicos.ai.usage import UsageContext, run_completion
from civicos.core.clock import utcnow
from civicos.core.i18n import LANGUAGES
from civicos.core.text import truncate
from civicos.domain.enums import ConversationRole, Visibility
from civicos.domain.knowledge import Conversation, ConversationMessage

logger = structlog.get_logger(__name__)

#: How many prior turns are replayed as context.
HISTORY_TURNS = 6
#: Below this mean retrieval score we tell the user we are unsure.
GROUNDING_FLOOR = 0.25


@dataclass(slots=True)
class AssistantReply:
    answer: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    conversation_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    grounded: bool = True
    grounding_score: float = 0.0
    confidence: float = 0.0
    escalate_to_human: bool = False
    follow_up_questions: list[str] = field(default_factory=list)
    model: str | None = None
    provider: str | None = None
    latency_ms: int = 0


async def ask(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    question: str,
    *,
    audience: Visibility = Visibility.PUBLIC,
    conversation: Conversation | None = None,
    language: str = "en",
    document_ids: list[uuid.UUID] | None = None,
    usage: UsageContext | None = None,
    persist: bool = True,
) -> AssistantReply:
    """Answer a question from the tenant's document corpus."""
    usage = usage or UsageContext.from_request(session, tenant_id=tenant_id)

    retrieval = await retrieve(
        session,
        tenant_id,
        question,
        audience=audience,
        document_ids=document_ids,
        usage=usage,
    )

    history = await _load_history(session, conversation) if conversation else []
    language_name = LANGUAGES.get(language, {}).get("name", "English")

    user_prompt = "\n\n".join(
        [
            f"Context documents:\n{context_block(retrieval.as_prompt_passages())}",
            f"Resident's question ({language_name}):\n{question}",
        ]
    )

    request = CompletionRequest(
        messages=[*history, Message(role="user", content=user_prompt)],
        system=ASSISTANT_SYSTEM,
        response_schema=GroundedAnswer.response_schema(),
        schema_name="grounded_answer",
        max_output_tokens=1600,
        temperature=0.1,
        context={"passages": len(retrieval.passages)},
    )

    result = await run_completion(request, capability="rag", usage=usage)

    try:
        answer = GroundedAnswer.model_validate(result.parsed or {})
    except Exception as exc:
        logger.warning("assistant_validation_failed", error=str(exc))
        answer = GroundedAnswer(
            answer=result.text.strip() or _no_answer_text(),
            answered_from_context=bool(retrieval.passages),
            citations=[],
            confidence=0.2,
            follow_up_questions=[],
            escalate_to_human=not retrieval.passages,
        )

    grounding = retrieval.mean_score
    if retrieval.is_empty or not answer.answered_from_context:
        answer = answer.model_copy(
            update={
                "answer": answer.answer or _no_answer_text(),
                "escalate_to_human": True,
            }
        )

    citations = _reconcile_citations(answer, retrieval)

    reply = AssistantReply(
        answer=answer.answer,
        citations=citations,
        grounded=answer.answered_from_context and not retrieval.is_empty,
        grounding_score=round(grounding, 4),
        confidence=answer.confidence,
        escalate_to_human=answer.escalate_to_human or grounding < GROUNDING_FLOOR,
        follow_up_questions=answer.follow_up_questions[:3],
        model=result.model,
        provider=result.provider,
        latency_ms=result.latency_ms,
    )

    if persist and conversation is not None:
        message = await _persist_turn(session, conversation, question, reply, result)
        reply.conversation_id = conversation.id
        reply.message_id = message.id

    return reply


async def get_or_create_conversation(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    conversation_id: uuid.UUID | None = None,
    session_key: str | None = None,
    user_id: uuid.UUID | None = None,
    language: str = "en",
    audience: Visibility = Visibility.PUBLIC,
) -> Conversation:
    """Resume a thread by id or by anonymous session key, else start one."""
    if conversation_id:
        conversation = await session.get(Conversation, conversation_id)
        if conversation and conversation.tenant_id == tenant_id:
            return conversation

    if session_key:
        existing = await session.scalar(
            select(Conversation)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.session_key == session_key,
                Conversation.deleted_at.is_(None),
            )
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
        if existing:
            return existing

    conversation = Conversation(
        tenant_id=tenant_id,
        user_id=user_id,
        session_key=session_key,
        language=language,
        audience=audience,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def _load_history(
    session: AsyncSession, conversation: Conversation
) -> list[Message]:
    rows = (
        await session.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.created_at.desc())
            .limit(HISTORY_TURNS * 2)
        )
    ).all()
    messages = [
        Message(
            role="assistant" if row.role is ConversationRole.ASSISTANT else "user",
            # Old turns are trimmed: they provide continuity, not evidence.
            content=truncate(row.content, 1500),
        )
        for row in reversed(rows)
        if row.role in {ConversationRole.USER, ConversationRole.ASSISTANT}
    ]
    return messages


async def _persist_turn(
    session: AsyncSession,
    conversation: Conversation,
    question: str,
    reply: AssistantReply,
    result: Any,
) -> ConversationMessage:
    session.add(
        ConversationMessage(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            role=ConversationRole.USER,
            content=question,
        )
    )
    message = ConversationMessage(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        role=ConversationRole.ASSISTANT,
        content=reply.answer,
        citations=reply.citations,
        model=result.model,
        provider=result.provider,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        latency_ms=result.latency_ms,
        grounding_score=reply.grounding_score,
    )
    session.add(message)

    conversation.message_count += 2
    conversation.total_input_tokens += result.usage.input_tokens
    conversation.total_output_tokens += result.usage.output_tokens
    conversation.last_message_at = utcnow()
    if not conversation.title:
        conversation.title = truncate(question, 120)
    await session.flush()
    return message


def _reconcile_citations(
    answer: GroundedAnswer, retrieval: RetrievalResult
) -> list[dict[str, Any]]:
    """Keep only citations that point at passages we actually retrieved.

    A model occasionally cites a plausible-looking document id it never saw.
    Publishing that to a resident would be a fabricated source, so unmatched
    citations are dropped and the retrieved set is used instead.
    """
    retrieved = {citation["document_id"]: citation for citation in retrieval.as_citations()}
    reconciled: list[dict[str, Any]] = []
    seen: set[str] = set()

    for citation in answer.citations:
        match = retrieved.get(citation.document_id)
        if match and citation.document_id not in seen:
            reconciled.append({**match, "quote": truncate(citation.quote, 300)})
            seen.add(citation.document_id)

    if not reconciled and answer.answered_from_context:
        reconciled = list(retrieved.values())[:3]
    return reconciled


def _no_answer_text() -> str:
    return (
        "I could not find that in the documents published by this administration. "
        "Please contact the relevant department directly, or file a request so a "
        "member of staff can respond."
    )
