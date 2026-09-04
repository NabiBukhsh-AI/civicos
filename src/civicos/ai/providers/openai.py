"""OpenAI-compatible provider.

Also covers self-hosted, OpenAI-compatible gateways (vLLM, Ollama's compat
layer, LiteLLM) by pointing ``base_url`` at them - useful for a municipality
with a data-residency requirement that rules out a hosted API.
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

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


class OpenAIProvider(LLMProvider):
    name = "openai"
    supports_vision = True
    supports_native_schema = True

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key or settings.ai.openai_api_key
        self._default_model = model or DEFAULT_MODEL
        self._base_url = base_url
        self._settings = settings
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ConfigurationError(
                "CIVICOS_AI__OPENAI_API_KEY is not set.", code="openai_api_key_missing"
            )
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ConfigurationError(
                "The 'openai' package is not installed. "
                "Install it with: pip install 'civicos[openai]'",
                code="openai_sdk_missing",
            ) from exc
        kwargs: dict[str, Any] = {
            "api_key": self._api_key,
            "timeout": float(self._settings.ai.request_timeout_seconds),
            "max_retries": self._settings.ai.max_retries,
        }
        if self._base_url:
            kwargs["base_url"] = self._base_url
        self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        client = self._get_client()
        model = request.model or self._default_model
        payload = self._build_payload(request, model)

        started = time.perf_counter()
        try:
            response = await client.chat.completions.create(**payload)
        except Exception as exc:
            raise AIProviderError(
                f"OpenAI request failed: {exc}", code="openai_request_failed"
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        choice = response.choices[0]
        text = choice.message.content or ""
        usage = response.usage

        result = CompletionResult(
            text=text,
            model=getattr(response, "model", model),
            provider=self.name,
            usage=TokenUsage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
            latency_ms=latency_ms,
            stop_reason=choice.finish_reason,
            raw=response,
        )
        if request.response_schema and text:
            parsed = extract_json(text)
            if parsed is not None:
                result.parsed = coerce_schema_defaults(parsed, request.response_schema)
        return result

    def _build_payload(self, request: CompletionRequest, model: str) -> dict[str, Any]:
        settings = self._settings
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})

        for message in request.messages:
            if message.images:
                parts: list[dict[str, Any]] = []
                if message.content:
                    parts.append({"type": "text", "text": message.content})
                parts.extend(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{image.media_type};base64,{image.to_base64()}"},
                    }
                    for image in message.images
                )
                messages.append({"role": message.role, "content": parts})
            else:
                messages.append({"role": message.role, "content": message.content})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": request.max_output_tokens or settings.ai.max_output_tokens,
            "temperature": (
                request.temperature if request.temperature is not None else settings.ai.temperature
            ),
        }
        if request.stop_sequences:
            payload["stop"] = request.stop_sequences
        if request.response_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "schema": request.response_schema,
                    "strict": True,
                },
            }
        return payload

    async def embed(self, texts: list[str], model: str | None = None) -> EmbeddingResult:
        client = self._get_client()
        model = model or self._settings.ai.embedding_model or DEFAULT_EMBEDDING_MODEL
        started = time.perf_counter()
        try:
            response = await client.embeddings.create(model=model, input=texts)
        except Exception as exc:
            raise AIProviderError(
                f"OpenAI embedding request failed: {exc}", code="openai_embed_failed"
            ) from exc
        usage = getattr(response, "usage", None)
        return EmbeddingResult(
            vectors=[item.embedding for item in response.data],
            model=model,
            provider=self.name,
            usage=TokenUsage(input_tokens=getattr(usage, "prompt_tokens", 0) or 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def health(self) -> bool:
        return bool(self._api_key)
