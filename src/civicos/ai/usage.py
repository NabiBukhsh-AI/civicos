"""Metered execution of AI calls.

Every model invocation in CivicOS goes through :func:`run_completion` or
:func:`run_embedding`. That single choke point gives us, for free:

* a per-tenant daily token budget that degrades to the offline provider
  instead of running up an unbounded bill,
* a per-call ledger row (who, what for, which model, how much it cost),
* Prometheus counters, and
* uniform error handling.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.ai.pricing import estimate_cost_usd
from civicos.ai.registry import model_for, provider_for
from civicos.ai.types import (
    Capability,
    CompletionRequest,
    CompletionResult,
    EmbeddingResult,
)
from civicos.core import context
from civicos.core.clock import utcnow
from civicos.core.config import get_settings
from civicos.core.errors import AIProviderError
from civicos.core.telemetry import record_ai_call
from civicos.domain.enums import AICapability
from civicos.domain.knowledge import AIUsage

logger = structlog.get_logger(__name__)

_CAPABILITY_MAP = {
    "chat": AICapability.CHAT,
    "rag": AICapability.RAG,
    "triage": AICapability.TRIAGE,
    "vision": AICapability.VISION,
    "summary": AICapability.SUMMARY,
    "translation": AICapability.TRANSLATION,
    "embedding": AICapability.EMBEDDING,
    "briefing": AICapability.BRIEFING,
    "duplicate_check": AICapability.DUPLICATE_CHECK,
    "moderation": AICapability.MODERATION,
}


@dataclass(slots=True)
class UsageContext:
    """Everything the ledger needs that the request itself does not carry."""

    session: AsyncSession | None = None
    tenant_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    label: str | None = None

    @classmethod
    def from_request(cls, session: AsyncSession | None = None, **kwargs: Any) -> UsageContext:
        actor = context.get_actor()
        return cls(
            session=session,
            tenant_id=kwargs.pop("tenant_id", None) or context.get_tenant_id(),
            user_id=kwargs.pop("user_id", None) or actor.id,
            department_id=kwargs.pop("department_id", None) or actor.department_id,
            **kwargs,
        )


async def run_completion(
    request: CompletionRequest,
    *,
    capability: str = "chat",
    usage: UsageContext | None = None,
    allow_fallback: bool = True,
) -> CompletionResult:
    """Execute a completion with budget enforcement, metering and fallback."""
    usage = usage or UsageContext.from_request()
    settings = get_settings()

    provider = provider_for(Capability.VISION if request.images else Capability.CHAT)
    if request.model is None:
        request.model = model_for(Capability.VISION if request.images else Capability.CHAT)

    if (
        usage.session is not None
        and usage.tenant_id is not None
        and await _budget_exhausted(usage.session, usage.tenant_id, settings.ai.daily_token_budget)
    ):
        # Degrade rather than overspend: the queue keeps moving on rules alone.
        logger.warning("ai_budget_exhausted_using_offline", tenant_id=str(usage.tenant_id))
        provider = _offline_provider()

    try:
        result = await provider.complete(request)
    except AIProviderError as exc:
        record_ai_call(provider.name, capability, outcome="error")
        await _record(usage, capability, provider.name, request.model or "", None, str(exc.code))
        if not allow_fallback:
            raise
        logger.warning("ai_provider_failed_falling_back", provider=provider.name, error=exc.code)
        fallback = _offline_provider()
        result = await fallback.complete(request)
        provider = fallback

    record_ai_call(
        provider.name,
        capability,
        outcome="ok",
        duration_seconds=result.latency_ms / 1000,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
    )
    await _record(usage, capability, provider.name, result.model, result, None)
    return result


async def run_embedding(
    texts: list[str],
    *,
    usage: UsageContext | None = None,
    model: str | None = None,
) -> EmbeddingResult:
    """Embed a batch of texts, falling back to the offline hashing embedder."""
    usage = usage or UsageContext.from_request()
    provider = provider_for(Capability.EMBEDDING)
    model = model or model_for(Capability.EMBEDDING)

    try:
        result = await provider.embed(texts, model=model)
    except AIProviderError as exc:
        logger.warning("embedding_failed_falling_back", provider=provider.name, error=str(exc))
        provider = _offline_provider()
        result = await provider.embed(texts)

    record_ai_call(
        provider.name,
        "embedding",
        outcome="ok",
        duration_seconds=result.latency_ms / 1000,
        input_tokens=result.usage.input_tokens,
    )
    await _record(usage, "embedding", provider.name, result.model, None, None, result=result)
    return result


def _offline_provider() -> Any:
    from civicos.ai.registry import get_provider

    return get_provider("heuristic")


async def _budget_exhausted(session: AsyncSession, tenant_id: uuid.UUID, daily_budget: int) -> bool:
    """True when today's token spend for this tenant exceeds the cap."""
    if daily_budget <= 0:
        return False
    since = utcnow() - timedelta(hours=24)
    total = await session.scalar(
        select(func.coalesce(func.sum(AIUsage.input_tokens + AIUsage.output_tokens), 0)).where(
            AIUsage.tenant_id == tenant_id, AIUsage.created_at >= since
        )
    )
    return int(total or 0) >= daily_budget


async def _record(
    usage: UsageContext,
    capability: str,
    provider: str,
    model: str,
    completion: CompletionResult | None,
    error_code: str | None,
    *,
    result: EmbeddingResult | None = None,
) -> None:
    """Append one row to the AI ledger. Never raises into the caller."""
    if usage.session is None or usage.tenant_id is None:
        return

    token_usage = completion.usage if completion else (result.usage if result else None)
    input_tokens = token_usage.input_tokens if token_usage else 0
    output_tokens = token_usage.output_tokens if token_usage else 0
    cached = token_usage.cached_input_tokens if token_usage else 0
    latency = completion.latency_ms if completion else (result.latency_ms if result else None)

    entry = AIUsage(
        tenant_id=usage.tenant_id,
        capability=_CAPABILITY_MAP.get(capability, AICapability.CHAT),
        provider=provider,
        model=model or "unknown",
        user_id=usage.user_id,
        department_id=usage.department_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency,
        estimated_cost_usd=estimate_cost_usd(model, input_tokens, output_tokens, cached),
        succeeded=error_code is None,
        error_code=error_code,
        entity_type=usage.entity_type,
        entity_id=usage.entity_id,
        details={"label": usage.label} if usage.label else {},
    )
    try:
        usage.session.add(entry)
        await usage.session.flush()
    except Exception as exc:  # pragma: no cover - ledger must never break a request
        logger.warning("ai_usage_ledger_write_failed", error=str(exc))


async def tenant_usage_summary(
    session: AsyncSession, tenant_id: uuid.UUID, days: int = 30
) -> dict[str, Any]:
    """Aggregate AI spend for the admin dashboard."""
    since = utcnow() - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                AIUsage.capability,
                func.count().label("calls"),
                func.sum(AIUsage.input_tokens).label("input_tokens"),
                func.sum(AIUsage.output_tokens).label("output_tokens"),
                func.sum(AIUsage.estimated_cost_usd).label("cost"),
            )
            .where(AIUsage.tenant_id == tenant_id, AIUsage.created_at >= since)
            .group_by(AIUsage.capability)
        )
    ).all()

    by_capability = [
        {
            "capability": str(row.capability),
            "calls": int(row.calls or 0),
            "input_tokens": int(row.input_tokens or 0),
            "output_tokens": int(row.output_tokens or 0),
            "estimated_cost_usd": round(float(row.cost or 0.0), 4),
        }
        for row in rows
    ]
    return {
        "period_days": days,
        "total_calls": sum(item["calls"] for item in by_capability),
        "total_tokens": sum(item["input_tokens"] + item["output_tokens"] for item in by_capability),
        "estimated_cost_usd": round(sum(item["estimated_cost_usd"] for item in by_capability), 4),
        "by_capability": by_capability,
    }
