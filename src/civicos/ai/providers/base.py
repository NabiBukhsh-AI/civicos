"""The provider contract.

A provider is a thin adapter over one vendor's SDK. It owns transport concerns
(auth, retries, response shape) and nothing else: no prompts, no business rules,
no persistence. That boundary is what keeps the rest of the AI layer testable
without a network.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

import structlog

from civicos.ai.types import CompletionRequest, CompletionResult, EmbeddingResult
from civicos.core.errors import AIProviderError

logger = structlog.get_logger(__name__)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_BARE_JSON = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


class LLMProvider(ABC):
    """Base class for chat/vision providers."""

    name: str = "base"
    supports_vision: bool = False
    supports_native_schema: bool = False

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Run one chat completion."""

    async def embed(self, texts: list[str], model: str | None = None) -> EmbeddingResult:
        raise AIProviderError(
            f"Provider '{self.name}' does not offer an embedding endpoint.",
            code="embeddings_unsupported",
        )

    async def health(self) -> bool:
        """Cheap liveness probe used by ``/health/ready``."""
        return True

    async def aclose(self) -> None:
        return None


def extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON recovery from a model response.

    Providers with native schema enforcement return clean JSON; this is the
    fallback for those that do not, and the safety net for a model that wraps
    valid JSON in prose or a fenced block. Returning ``None`` rather than
    raising lets callers degrade to a heuristic instead of failing a request.
    """
    if not text:
        return None
    candidate = text.strip()

    for attempt in (candidate, *_json_candidates(candidate)):
        try:
            parsed = json.loads(attempt)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
    logger.debug("json_extraction_failed", preview=candidate[:200])
    return None


def _json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    if match := _JSON_BLOCK.search(text):
        candidates.append(match.group(1))
    if match := _BARE_JSON.search(text):
        candidates.append(match.group(1))
    return candidates


def coerce_schema_defaults(payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Fill required keys a model omitted, so downstream code sees a full shape."""
    properties: dict[str, Any] = schema.get("properties", {})
    for key in schema.get("required", []):
        if key in payload:
            continue
        spec = properties.get(key, {})
        payload[key] = _empty_for(spec)
    return payload


def _empty_for(spec: dict[str, Any]) -> Any:
    kind = spec.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    return {
        "array": [],
        "object": {},
        "string": "",
        "integer": 0,
        "number": 0.0,
        "boolean": False,
    }.get(kind)
