"""Token pricing, so AI spend appears in the same ledger as everything else.

Rates are USD per million tokens and are a *local, editable* table - a
municipality on an enterprise agreement edits one dict rather than patching
call sites. Unknown models fall back to a conservative estimate so cost is
never silently reported as zero.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelRate:
    """USD per 1M tokens."""

    input_per_million: float
    output_per_million: float
    cached_input_per_million: float | None = None


#: Prefix-matched so dated snapshots inherit their family's rate.
MODEL_RATES: dict[str, ModelRate] = {
    # Anthropic
    "claude-opus-5": ModelRate(5.00, 25.00, 0.50),
    "claude-opus-4": ModelRate(5.00, 25.00, 0.50),
    "claude-sonnet-5": ModelRate(2.00, 10.00, 0.20),
    "claude-sonnet-4": ModelRate(3.00, 15.00, 0.30),
    "claude-haiku-4-5": ModelRate(1.00, 5.00, 0.10),
    "claude-fable-5": ModelRate(10.00, 50.00, 1.00),
    # Google
    "gemini-2.0-flash": ModelRate(0.10, 0.40),
    "gemini-2.5-flash": ModelRate(0.30, 2.50),
    "gemini-2.5-pro": ModelRate(1.25, 10.00),
    "text-embedding-004": ModelRate(0.02, 0.0),
    # OpenAI
    "gpt-4o-mini": ModelRate(0.15, 0.60),
    "gpt-4o": ModelRate(2.50, 10.00),
    "text-embedding-3-small": ModelRate(0.02, 0.0),
    "text-embedding-3-large": ModelRate(0.13, 0.0),
    # Offline
    "heuristic-v1": ModelRate(0.0, 0.0),
    "hashing-trick-v1": ModelRate(0.0, 0.0),
}

#: Used when a model id is not in the table at all.
FALLBACK_RATE = ModelRate(3.00, 15.00)


def rate_for(model: str) -> ModelRate:
    if model in MODEL_RATES:
        return MODEL_RATES[model]
    # Longest prefix wins, so "claude-opus-5-20260101" resolves to the Opus 5 rate.
    matches = [key for key in MODEL_RATES if model.startswith(key)]
    if matches:
        return MODEL_RATES[max(matches, key=len)]
    return FALLBACK_RATE


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float:
    """Estimated USD cost of one call, rounded to the nearest hundredth of a cent."""
    rate = rate_for(model)
    billable_input = max(input_tokens - cached_input_tokens, 0)
    cost = (
        billable_input * rate.input_per_million
        + output_tokens * rate.output_per_million
        + cached_input_tokens * (rate.cached_input_per_million or rate.input_per_million)
    ) / 1_000_000
    return round(cost, 6)


def format_cost(amount_usd: float) -> str:
    if amount_usd < 0.01:
        return f"${amount_usd:.4f}"
    return f"${amount_usd:,.2f}"
