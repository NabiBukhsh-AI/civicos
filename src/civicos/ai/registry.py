"""Provider registry and capability routing.

One place decides which vendor serves chat, which serves vision and which
serves embeddings, and one place implements the fallback to the offline
heuristic provider. Callers ask for a capability, never for a vendor.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

from civicos.ai.providers.base import LLMProvider
from civicos.ai.providers.heuristic import HeuristicProvider
from civicos.ai.types import Capability
from civicos.core.config import get_settings
from civicos.core.errors import ConfigurationError

logger = structlog.get_logger(__name__)

_FACTORIES: dict[str, Callable[[], LLMProvider]] = {}
_INSTANCES: dict[str, LLMProvider] = {}


def _register_defaults() -> None:
    if _FACTORIES:
        return

    def _anthropic() -> LLMProvider:
        from civicos.ai.providers.anthropic import AnthropicProvider

        return AnthropicProvider()

    def _google() -> LLMProvider:
        from civicos.ai.providers.google import GoogleProvider

        return GoogleProvider()

    def _openai() -> LLMProvider:
        from civicos.ai.providers.openai import OpenAIProvider

        return OpenAIProvider()

    _FACTORIES.update(
        {
            "anthropic": _anthropic,
            "google": _google,
            "openai": _openai,
            "mock": HeuristicProvider,
            "heuristic": HeuristicProvider,
        }
    )


def register_provider(name: str, factory: Callable[[], LLMProvider]) -> None:
    """Register a custom provider (a local model gateway, a mock in tests)."""
    _register_defaults()
    _FACTORIES[name] = factory
    _INSTANCES.pop(name, None)


def get_provider(name: str) -> LLMProvider:
    """Return the named provider, constructing it on first use."""
    _register_defaults()
    if name in _INSTANCES:
        return _INSTANCES[name]
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ConfigurationError(
            f"Unknown AI provider '{name}'. Known providers: {', '.join(sorted(_FACTORIES))}.",
            code="unknown_ai_provider",
        )
    provider = factory()
    _INSTANCES[name] = provider
    return provider


def provider_for(capability: Capability | str) -> LLMProvider:
    """Resolve the provider that should serve a capability.

    Falls back to the offline heuristic provider when the configured vendor is
    unavailable (SDK not installed, no credentials) rather than failing the
    request: a municipality's intake queue must keep working.
    """
    settings = get_settings()
    capability = Capability(capability)

    if capability is Capability.EMBEDDING:
        name = settings.ai.resolved_embedding_provider
    elif capability is Capability.VISION:
        name = settings.ai.resolved_vision_provider
    else:
        name = settings.ai.provider

    try:
        provider = get_provider(name)
    except ConfigurationError:
        logger.warning("ai_provider_unavailable_falling_back", requested=name)
        return get_provider("heuristic")

    if capability is Capability.VISION and not provider.supports_vision:
        logger.warning("provider_lacks_vision_falling_back", provider=provider.name)
        return get_provider("heuristic")
    return provider


def model_for(capability: Capability | str) -> str | None:
    """The model id configured for a capability, if any."""
    settings = get_settings()
    capability = Capability(capability)
    if capability is Capability.EMBEDDING:
        return settings.ai.embedding_model
    if capability is Capability.VISION:
        return settings.ai.vision_model or settings.ai.chat_model
    return settings.ai.chat_model


def is_ai_enabled() -> bool:
    """True when a real (non-heuristic) provider is configured."""
    return get_settings().ai.provider not in {"mock", "heuristic"}


async def reset_providers() -> None:
    """Drop cached provider instances (used on shutdown and between tests)."""
    for provider in list(_INSTANCES.values()):
        await provider.aclose()
    _INSTANCES.clear()


async def provider_health() -> dict[str, bool]:
    """Health snapshot for the readiness probe."""
    settings = get_settings()
    names = {
        settings.ai.provider,
        settings.ai.resolved_vision_provider,
        settings.ai.resolved_embedding_provider,
    }
    health: dict[str, bool] = {}
    for name in names:
        try:
            health[name] = await get_provider(name).health()
        except ConfigurationError:
            health[name] = False
    return health
