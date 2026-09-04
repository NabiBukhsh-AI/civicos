"""Provider-neutral request/response types for the AI layer.

Everything above :mod:`civicos.ai.providers` speaks these dataclasses, so
swapping Claude for Gemini (or for a locally hosted model) is a configuration
change rather than a code change.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

Role = Literal["system", "user", "assistant"]


class Capability(StrEnum):
    """What a provider is being asked to do - used for routing and metering."""

    CHAT = "chat"
    STRUCTURED = "structured"
    VISION = "vision"
    EMBEDDING = "embedding"


@dataclass(slots=True)
class ImagePart:
    """An image ready to be sent to a multimodal model."""

    data: bytes
    media_type: str = "image/jpeg"
    label: str | None = None

    def to_base64(self) -> str:
        return base64.standard_b64encode(self.data).decode("ascii")

    @property
    def size_bytes(self) -> int:
        return len(self.data)


@dataclass(slots=True)
class Message:
    role: Role
    content: str
    images: list[ImagePart] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class CompletionRequest:
    """One call to a chat model."""

    messages: list[Message]
    system: str | None = None
    model: str | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    #: JSON Schema. When set, providers constrain the response to match it.
    response_schema: dict[str, Any] | None = None
    schema_name: str = "response"
    stop_sequences: list[str] = field(default_factory=list)
    capability: Capability = Capability.CHAT
    #: Free-form hints (issue id, tenant, department) recorded in the usage ledger.
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def images(self) -> list[ImagePart]:
        return [image for message in self.messages for image in message.images]


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
        )


@dataclass(slots=True)
class CompletionResult:
    text: str
    model: str
    provider: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    stop_reason: str | None = None
    #: Populated when ``response_schema`` was supplied and parsing succeeded.
    parsed: dict[str, Any] | None = None
    raw: Any = None

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


@dataclass(slots=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    provider: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0

    @property
    def dimensions(self) -> int:
        return len(self.vectors[0]) if self.vectors else 0
