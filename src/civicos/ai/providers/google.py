"""Google Gemini provider.

Kept first-class because the original Municipal AI Assistant ran on Gemini and
because Gemini's embedding endpoint is a practical default for deployments with
Google Cloud credits. Uses the current ``google-genai`` SDK.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from civicos.ai.providers.base import LLMProvider, coerce_schema_defaults, extract_json
from civicos.ai.types import (
    CompletionRequest,
    CompletionResult,
    EmbeddingResult,
    TokenUsage,
)
from civicos.core.config import get_settings
from civicos.core.errors import AIProviderError, ConfigurationError

logger = structlog.get_logger(__name__)

DEFAULT_MODEL = "gemini-2.0-flash"
DEFAULT_EMBEDDING_MODEL = "text-embedding-004"


class GoogleProvider(LLMProvider):
    name = "google"
    supports_vision = True
    supports_native_schema = True

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self._api_key = api_key or settings.ai.google_api_key
        self._default_model = model or DEFAULT_MODEL
        self._settings = settings
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ConfigurationError(
                "CIVICOS_AI__GOOGLE_API_KEY is not set.", code="google_api_key_missing"
            )
        try:
            from google import genai  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ConfigurationError(
                "The 'google-genai' package is not installed. "
                "Install it with: pip install 'civicos[google]'",
                code="google_sdk_missing",
            ) from exc
        self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        client = self._get_client()
        model = request.model or self._default_model
        contents, config = self._build_payload(request)

        started = time.perf_counter()
        try:
            response = await client.aio.models.generate_content(
                model=model, contents=contents, config=config
            )
        except Exception as exc:  # google-genai raises vendor-specific errors
            raise AIProviderError(
                f"Gemini request failed: {exc}", code="google_request_failed"
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        text = getattr(response, "text", "") or ""
        metadata = getattr(response, "usage_metadata", None)

        result = CompletionResult(
            text=text,
            model=model,
            provider=self.name,
            usage=TokenUsage(
                input_tokens=getattr(metadata, "prompt_token_count", 0) or 0,
                output_tokens=getattr(metadata, "candidates_token_count", 0) or 0,
                cached_input_tokens=getattr(metadata, "cached_content_token_count", 0) or 0,
            ),
            latency_ms=latency_ms,
            raw=response,
        )
        if request.response_schema and text:
            parsed = extract_json(text)
            if parsed is not None:
                result.parsed = coerce_schema_defaults(parsed, request.response_schema)
        return result

    def _build_payload(self, request: CompletionRequest) -> tuple[list[Any], dict[str, Any]]:
        from google.genai import types as genai_types  # noqa: PLC0415

        settings = self._settings
        system_parts: list[str] = [request.system] if request.system else []
        contents: list[Any] = []

        for message in request.messages:
            if message.role == "system":
                system_parts.append(message.content)
                continue
            parts: list[Any] = []
            for image in message.images:
                parts.append(
                    genai_types.Part.from_bytes(
                        data=image.data, mime_type=image.media_type
                    )
                )
            if message.content:
                parts.append(genai_types.Part.from_text(text=message.content))
            contents.append(
                genai_types.Content(
                    role="model" if message.role == "assistant" else "user", parts=parts
                )
            )

        config: dict[str, Any] = {
            "max_output_tokens": request.max_output_tokens or settings.ai.max_output_tokens,
            "temperature": (
                request.temperature
                if request.temperature is not None
                else settings.ai.temperature
            ),
        }
        if system_parts:
            config["system_instruction"] = "\n\n".join(system_parts)
        if request.stop_sequences:
            config["stop_sequences"] = request.stop_sequences
        if request.response_schema:
            config["response_mime_type"] = "application/json"
            config["response_schema"] = _to_gemini_schema(request.response_schema)
        return contents, config

    async def embed(self, texts: list[str], model: str | None = None) -> EmbeddingResult:
        client = self._get_client()
        model = model or self._settings.ai.embedding_model or DEFAULT_EMBEDDING_MODEL
        started = time.perf_counter()
        try:
            response = await client.aio.models.embed_content(model=model, contents=texts)
        except Exception as exc:
            raise AIProviderError(
                f"Gemini embedding request failed: {exc}", code="google_embed_failed"
            ) from exc
        vectors = [list(item.values) for item in response.embeddings]
        return EmbeddingResult(
            vectors=vectors,
            model=model,
            provider=self.name,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def health(self) -> bool:
        return bool(self._api_key)


def _to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Strip JSON-Schema keywords the Gemini schema dialect rejects."""
    unsupported = {"additionalProperties", "$schema", "definitions", "$defs", "title"}
    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in unsupported:
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {
                prop: _to_gemini_schema(spec) if isinstance(spec, dict) else spec
                for prop, spec in value.items()
            }
        elif key == "items" and isinstance(value, dict):
            cleaned[key] = _to_gemini_schema(value)
        else:
            cleaned[key] = value
    return cleaned
