"""Offline, deterministic provider - the platform's floor, not a stub.

Two jobs:

1. **Tests and CI** run the entire stack with no API key and no network.
2. **Degraded production.** A municipality that has not procured an AI vendor
   (or whose key has expired, or whose budget cap has been hit) still gets
   working triage, search and duplicate detection - just rule-based instead of
   model-based. Nothing in the product hard-fails because the LLM is absent.

Embeddings use the hashing trick over character n-grams and word tokens, which
gives genuinely useful cosine similarity for near-duplicate complaint text
without any model at all.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from itertools import pairwise
from typing import Any

from civicos.ai.providers.base import LLMProvider, coerce_schema_defaults
from civicos.ai.types import (
    CompletionRequest,
    CompletionResult,
    EmbeddingResult,
    TokenUsage,
)
from civicos.core.config import get_settings
from civicos.core.text import summarise_for_title, tokenize, truncate

#: Keyword -> (category slug, priority, severity). Ordered by specificity.
_CATEGORY_RULES: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
    (("collapse", "collapsed", "cave-in", "sinkhole"), "roads", "emergency", "critical"),
    (("fire", "smoke", "burning", "gas leak"), "fire-safety", "emergency", "critical"),
    (("electrocut", "live wire", "shock", "sparking"), "streetlights", "emergency", "critical"),
    (("sewer", "sewage", "gutter", "manhole", "overflow"), "sewerage", "high", "major"),
    (("drain", "flood", "waterlogg", "standing water", "rain water"), "drainage", "high", "major"),
    (
        ("garbage", "trash", "rubbish", "waste", "dump", "litter"),
        "solid-waste",
        "normal",
        "moderate",
    ),
    (("water supply", "no water", "tanker", "pipeline", "leak"), "water-supply", "high", "major"),
    (("pothole", "road", "street repair", "pavement", "footpath"), "roads", "normal", "moderate"),
    (("streetlight", "street light", "lamp", "pole", "dark"), "streetlights", "normal", "minor"),
    (
        ("encroach", "illegal construction", "occupied", "kiosk"),
        "encroachment",
        "normal",
        "moderate",
    ),
    (("park", "playground", "tree", "green belt"), "parks", "low", "minor"),
    (("stray", "dog", "animal", "cattle"), "animal-control", "normal", "moderate"),
    (("mosquito", "dengue", "fogging", "spray"), "public-health", "high", "moderate"),
    (("noise", "loudspeaker", "generator"), "noise", "low", "minor"),
    (("toilet", "sanitation", "washroom"), "sanitation", "normal", "moderate"),
)

_URGENCY_WORDS = (
    "urgent",
    "emergency",
    "immediately",
    "danger",
    "dangerous",
    "injur",
    "accident",
    "child",
    "school",
    "hospital",
    "weeks",
    "months",
    "repeatedly",
)

_TOKEN_SPLIT = re.compile(r"[\w؀-ۿ]+", re.UNICODE)


class HeuristicProvider(LLMProvider):
    """Rule-based provider. Deterministic, instant, free."""

    name = "heuristic"
    supports_vision = True
    supports_native_schema = True

    def __init__(self, dimensions: int | None = None) -> None:
        settings = get_settings()
        self._dimensions = dimensions or settings.ai.embedding_dimensions

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        started = time.perf_counter()
        # `classify_text` lets a caller name the text that should drive rule
        # matching. Without it we would classify the whole prompt, taxonomy
        # listing included, and match keywords belonging to the instructions
        # rather than to the resident's own words.
        prompt = str(
            request.context.get("classify_text")
            or "\n".join(m.content for m in request.messages if m.role != "system")
        )
        image_count = len(request.images)

        if request.response_schema:
            payload = self._structured(request, prompt, image_count)
            text = _dump_json(payload)
            parsed: dict[str, Any] | None = coerce_schema_defaults(payload, request.response_schema)
        else:
            text = self._prose(prompt, image_count)
            parsed = None

        return CompletionResult(
            text=text,
            model="heuristic-v1",
            provider=self.name,
            usage=TokenUsage(
                input_tokens=len(prompt) // 4,
                output_tokens=len(text) // 4,
            ),
            latency_ms=int((time.perf_counter() - started) * 1000),
            stop_reason="end_turn",
            parsed=parsed,
        )

    # -- structured shapes ----------------------------------------------------

    def _structured(
        self, request: CompletionRequest, prompt: str, image_count: int
    ) -> dict[str, Any]:
        schema = request.response_schema or {}
        properties = set(schema.get("properties", {}))
        slug, priority, severity, matched = classify(prompt)

        payload: dict[str, Any] = {}
        if "category_slug" in properties:
            payload["category_slug"] = slug
        if "priority" in properties:
            payload["priority"] = priority
        if "severity" in properties:
            payload["severity"] = severity
        if "confidence" in properties:
            payload["confidence"] = 0.55 if matched else 0.25
        if "summary" in properties:
            payload["summary"] = summarise_for_title(prompt, 160) or "Civic issue reported."
        if "title" in properties:
            payload["title"] = summarise_for_title(prompt, 80) or "Civic issue"
        if "reasoning" in properties:
            payload["reasoning"] = (
                f"Matched keyword rules for '{slug}'."
                if matched
                else "No keyword rule matched; defaulted to general intake."
            )
        if "is_emergency" in properties:
            payload["is_emergency"] = priority == "emergency"
        if "requires_field_visit" in properties:
            payload["requires_field_visit"] = severity in {"major", "critical"}
        if "suggested_department_slug" in properties:
            payload["suggested_department_slug"] = _DEPARTMENT_FOR.get(slug)
        if "tags" in properties:
            payload["tags"] = [slug] + ([m for m in ("urgent",) if _is_urgent(prompt)])
        if "duplicate_likelihood" in properties:
            payload["duplicate_likelihood"] = 0.0
        if "language" in properties:
            payload["language"] = "en"
        if "translated_text" in properties:
            payload["translated_text"] = prompt.strip()
        if "sentiment" in properties:
            payload["sentiment"] = _sentiment(prompt)
        if "themes" in properties:
            payload["themes"] = [slug]
        if "answer" in properties:
            payload["answer"] = self._prose(prompt, image_count)
        if "citations" in properties:
            payload["citations"] = []
        if "observations" in properties:
            payload["observations"] = (
                [f"{image_count} image(s) attached; automated description unavailable offline."]
                if image_count
                else ["No imagery supplied."]
            )
        if "hazards" in properties:
            payload["hazards"] = ["Unverified - manual inspection required."]
        if "recommended_actions" in properties:
            payload["recommended_actions"] = [
                "Schedule a field inspection to confirm the reported condition."
            ]
        if "condition" in properties:
            payload["condition"] = "fair"
        if "headline" in properties:
            payload["headline"] = "Operational summary (rule-based)"
        if "key_points" in properties:
            payload["key_points"] = ["Generated without an AI provider configured."]
        if "risks" in properties:
            payload["risks"] = []
        return coerce_schema_defaults(payload, schema) if schema else payload

    def _prose(self, prompt: str, image_count: int) -> str:
        slug, priority, _severity, matched = classify(prompt)
        pretty = slug.replace("-", " ")
        lines = [
            "AI assistance is not configured on this deployment, so this is a rule-based response.",
        ]
        if matched:
            lines.append(
                f"The text matches the '{pretty}' category and suggests {priority} priority."
            )
        else:
            lines.append("No category rule matched; the request needs manual review.")
        if image_count:
            lines.append(f"{image_count} image(s) were received and stored with the record.")
        lines.append(
            "Configure CIVICOS_AI__PROVIDER and the matching API key to enable "
            "model-generated answers."
        )
        return " ".join(lines)

    # -- embeddings -----------------------------------------------------------

    async def embed(self, texts: list[str], model: str | None = None) -> EmbeddingResult:
        started = time.perf_counter()
        vectors = [hashing_embedding(text, self._dimensions) for text in texts]
        return EmbeddingResult(
            vectors=vectors,
            model=model or "hashing-trick-v1",
            provider=self.name,
            usage=TokenUsage(input_tokens=sum(len(t) // 4 for t in texts)),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def health(self) -> bool:
        return True


_DEPARTMENT_FOR = {
    "solid-waste": "sanitation",
    "sanitation": "sanitation",
    "sewerage": "water-sanitation",
    "water-supply": "water-sanitation",
    "drainage": "water-sanitation",
    "roads": "works",
    "streetlights": "works",
    "parks": "parks-horticulture",
    "encroachment": "enforcement",
    "animal-control": "public-health",
    "public-health": "public-health",
    "fire-safety": "emergency-services",
    "noise": "enforcement",
}


def classify(text: str) -> tuple[str, str, str, bool]:
    """Return ``(category_slug, priority, severity, matched)`` for free text."""
    lowered = (text or "").lower()
    for keywords, slug, priority, severity in _CATEGORY_RULES:
        if any(keyword in lowered for keyword in keywords):
            if priority != "emergency" and _is_urgent(lowered):
                priority = {"low": "normal", "normal": "high", "high": "urgent"}.get(
                    priority, priority
                )
            return slug, priority, severity, True
    return "other", "normal", "moderate", False


def _is_urgent(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _URGENCY_WORDS)


_POSITIVE = ("thank", "good", "great", "resolved", "appreciate", "excellent", "quick")
_NEGATIVE = ("worst", "terrible", "useless", "angry", "ignored", "never", "fed up", "again")


def _sentiment(text: str) -> str:
    lowered = (text or "").lower()
    positive = sum(word in lowered for word in _POSITIVE)
    negative = sum(word in lowered for word in _NEGATIVE)
    if positive and negative:
        return "mixed"
    if positive:
        return "positive"
    if negative:
        return "negative"
    return "neutral"


def hashing_embedding(text: str, dimensions: int) -> list[float]:
    """Deterministic sparse embedding via the hashing trick.

    Combines word unigrams, word bigrams and character 4-grams so that
    "garbage not collected in street 5" and "street 5 garbage uncollected"
    land close together. Sub-linear in text length and needs no model, which
    is what makes offline duplicate detection viable.
    """
    if not text:
        return [0.0] * dimensions

    words = tokenize(text, drop_stopwords=True) or _TOKEN_SPLIT.findall(text.lower())
    features: Counter[str] = Counter()
    features.update(words)
    features.update(f"{a}_{b}" for a, b in pairwise(words))

    compact = re.sub(r"\s+", " ", text.lower())
    features.update(compact[i : i + 4] for i in range(0, max(len(compact) - 3, 0)))

    vector = [0.0] * dimensions
    for feature, count in features.items():
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        # Sub-linear term weighting keeps one repeated word from dominating.
        vector[index] += sign * (1.0 + math.log(count))

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


def _dump_json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


def preview(text: str, limit: int = 120) -> str:
    return truncate(text, limit)
