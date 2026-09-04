"""Anthropic (Claude) provider.

Uses the official ``anthropic`` SDK's async client against the Messages API.
Structured output goes through ``output_config.format`` (native JSON-schema
enforcement) rather than prompt-and-pray parsing.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from civicos.ai.providers.base import LLMProvider, coerce_schema_defaults, extract_json
from civicos.ai.types import CompletionRequest, CompletionResult, TokenUsage
from civicos.core.config import get_settings
from civicos.core.errors import AIProviderError, ConfigurationError

logger = structlog.get_logger(__name__)

DEFAULT_MODEL = "claude-opus-5"


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    supports_vision = True
    supports_native_schema = True

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self._api_key = api_key or settings.ai.anthropic_api_key
        self._default_model = model or settings.ai.chat_model or DEFAULT_MODEL
        self._settings = settings
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ConfigurationError(
                "The 'anthropic' package is not installed. "
                "Install it with: pip install 'civicos[anthropic]'",
                code="anthropic_sdk_missing",
            ) from exc

        # A missing key is not fatal here: the SDK also resolves credentials from
        # ANTHROPIC_AUTH_TOKEN and from an `ant auth login` profile on disk.
        kwargs: dict[str, Any] = {
            "timeout": float(self._settings.ai.request_timeout_seconds),
            "max_retries": self._settings.ai.max_retries,
        }
        if self._api_key:
            kwargs["api_key"] = self._api_key
        self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        import anthropic

        client = self._get_client()
        model = request.model or self._default_model
        payload = self._build_payload(request, model)

        started = time.perf_counter()
        try:
            response = await client.messages.create(**payload)
        except anthropic.AuthenticationError as exc:
            raise ConfigurationError(
                "Anthropic credentials are missing or invalid.", code="anthropic_auth"
            ) from exc
        except anthropic.RateLimitError as exc:
            raise AIProviderError(
                "Anthropic rate limit reached; retry shortly.", code="anthropic_rate_limited"
            ) from exc
        except anthropic.APIStatusError as exc:
            raise AIProviderError(
                f"Anthropic returned {exc.status_code}.", code="anthropic_status_error"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise AIProviderError(
                "Could not reach the Anthropic API.", code="anthropic_unreachable"
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            # Surfaced rather than swallowed: the caller decides whether to fall
            # back to a rule-based path or tell the operator.
            details = getattr(response, "stop_details", None)
            logger.warning("anthropic_refusal", category=getattr(details, "category", None))

        usage = response.usage
        result = CompletionResult(
            text=text,
            model=getattr(response, "model", model),
            provider=self.name,
            usage=TokenUsage(
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                cached_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            ),
            latency_ms=latency_ms,
            stop_reason=stop_reason,
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
        system_parts: list[str] = []
        if request.system:
            system_parts.append(request.system)

        for message in request.messages:
            if message.role == "system":
                system_parts.append(message.content)
                continue
            if message.images:
                blocks: list[dict[str, Any]] = [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image.media_type,
                            "data": image.to_base64(),
                        },
                    }
                    for image in message.images
                ]
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                messages.append({"role": message.role, "content": blocks})
            else:
                messages.append({"role": message.role, "content": message.content})

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_output_tokens or settings.ai.max_output_tokens,
            "messages": messages,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.stop_sequences:
            payload["stop_sequences"] = request.stop_sequences
        if request.response_schema:
            payload["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": request.response_schema,
                }
            }
        return payload

    async def health(self) -> bool:
        try:
            self._get_client()
        except ConfigurationError:
            return False
        return bool(self._api_key)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
