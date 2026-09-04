"""Text utilities: slugs, normalisation, similarity, redaction, script detection.

Deliberately dependency-free. Everything here runs on every complaint that
enters the system, so it must be fast and must never call the network.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_WHITESPACE = re.compile(r"\s+")
_WORD = re.compile(r"[\w؀-ۿ]+", re.UNICODE)

# Very small stopword list; only used to sharpen duplicate detection.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "there",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "please",
        "kindly",
        "sir",
        "we",
        "our",
        "my",
        "me",
        "you",
        "your",
    ]
)

_URDU_RANGE = (0x0600, 0x06FF)
_ARABIC_SUPPLEMENT = (0x0750, 0x077F)


def slugify(value: str, *, max_length: int = 64, fallback: str = "item") -> str:
    """ASCII slug suitable for URLs and tenant subdomains."""
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", ascii_only).strip("-")
    slug = slug[:max_length].strip("-")
    return slug or fallback


def normalise_whitespace(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def truncate(value: str, limit: int, suffix: str = "...") -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - len(suffix))].rstrip() + suffix


def tokenize(value: str, *, drop_stopwords: bool = True) -> list[str]:
    tokens = [token.lower() for token in _WORD.findall(value or "")]
    if drop_stopwords:
        tokens = [token for token in tokens if token not in _STOPWORDS and len(token) > 1]
    return tokens


def token_set(value: str) -> frozenset[str]:
    return frozenset(tokenize(value))


def jaccard_similarity(left: str, right: str) -> float:
    """Cheap lexical similarity used as a first-pass duplicate filter."""
    a, b = token_set(left), token_set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    """Cosine similarity for plain Python sequences (no numpy import cost)."""
    a = list(left)
    b = list(right)
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def content_fingerprint(*parts: str | None) -> str:
    """Stable hash used to collapse byte-identical resubmissions."""
    joined = "|".join(normalise_whitespace(part or "").lower() for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def detect_script(value: str) -> str:
    """Return ``latin``, ``arabic`` (Urdu/Sindhi/Balochi) or ``mixed``."""
    arabic = latin = 0
    for char in value:
        code = ord(char)
        if _URDU_RANGE[0] <= code <= _URDU_RANGE[1] or (
            _ARABIC_SUPPLEMENT[0] <= code <= _ARABIC_SUPPLEMENT[1]
        ):
            arabic += 1
        elif char.isalpha() and code < 0x0250:
            latin += 1
    if arabic and latin:
        return "mixed"
    if arabic:
        return "arabic"
    return "latin"


def guess_language(value: str, default: str = "en") -> str:
    """Coarse language guess.

    Distinguishing Urdu from Sindhi from Balochi reliably needs a model; this
    only separates Arabic-script input from Latin so the pipeline can decide
    whether to call the translation step at all.
    """
    script = detect_script(value)
    if script in {"arabic", "mixed"}:
        return "ur"
    return default


_PHONE = re.compile(r"(?<!\d)(?:\+?92|0)?3\d{2}[-\s]?\d{7}(?!\d)")
_CNIC = re.compile(r"(?<!\d)\d{5}[-\s]?\d{7}[-\s]?\d(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def redact_pii(value: str) -> str:
    """Strip phone numbers, national ID numbers and emails from free text.

    Applied before complaint text is sent to an LLM or published on the open
    data feed, so that a resident who typed their CNIC into the description
    does not have it leave the building.
    """
    if not value:
        return value
    value = _CNIC.sub("[cnic-redacted]", value)
    value = _PHONE.sub("[phone-redacted]", value)
    return _EMAIL.sub("[email-redacted]", value)


def contains_pii(value: str) -> bool:
    return bool(_CNIC.search(value) or _PHONE.search(value) or _EMAIL.search(value))


_ABUSIVE = re.compile(
    r"\b(fuck|shit|bastard|bloody\s+fool|idiot|haram\s?khor|kutta|kamina)\b", re.IGNORECASE
)


def looks_abusive(value: str) -> bool:
    """Flag obviously abusive text for moderation review (not auto-rejection)."""
    return bool(_ABUSIVE.search(value or ""))


def summarise_for_title(value: str, limit: int = 90) -> str:
    """Derive a short title from a long complaint body."""
    text = normalise_whitespace(value)
    sentence = re.split(r"(?<=[.!?۔])\s", text, maxsplit=1)[0]
    return truncate(sentence or text, limit)
