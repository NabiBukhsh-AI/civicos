"""Minimal localisation layer.

A single municipality often serves several language communities on the same
street, so every citizen-facing string is looked up through this catalogue
rather than hard-coded. The catalogue is intentionally tiny and dependency-free;
adding a language is adding a dict, and an unknown key falls back to English so
a missing translation can never break a response.
"""

from __future__ import annotations

from typing import Any

from civicos.core.config import get_settings
from civicos.core.context import get_language

#: ISO code -> human label, used by the ``/meta/languages`` endpoint.
LANGUAGES: dict[str, dict[str, str]] = {
    "en": {"name": "English", "native_name": "English", "direction": "ltr"},
    "ur": {"name": "Urdu", "native_name": "اردو", "direction": "rtl"},
    "sd": {"name": "Sindhi", "native_name": "سنڌي", "direction": "rtl"},
    "bal": {"name": "Balochi", "native_name": "بلۏچی", "direction": "rtl"},
}

CATALOGUE: dict[str, dict[str, str]] = {
    "en": {
        "issue.created": "Your report {reference} has been received.",
        "issue.assigned": "Report {reference} has been assigned to {department}.",
        "issue.in_progress": "Work has started on your report {reference}.",
        "issue.resolved": "Report {reference} has been marked resolved. Reply to reopen it.",
        "issue.rejected": "Report {reference} was closed without action: {reason}",
        "issue.duplicate": "Report {reference} was merged into an existing report {parent}.",
        "issue.sla_breach": "Report {reference} has breached its {stage} deadline.",
        "issue.escalated": "Report {reference} has been escalated to {level}.",
        "workorder.assigned": "Work order {reference} has been assigned to you.",
        "workorder.completed": "Work order {reference} was completed.",
        "service.approved": "Your application {reference} has been approved.",
        "service.rejected": "Your application {reference} was rejected: {reason}",
        "service.info_required": "More information is required for application {reference}.",
        "alert.emergency": "EMERGENCY: {message}",
        "survey.invite": "Your feedback is requested on {subject}.",
        "generic.thanks": "Thank you for helping improve your neighbourhood.",
    },
    "ur": {
        "issue.created": "آپ کی شکایت {reference} موصول ہو گئی ہے۔",
        "issue.assigned": "شکایت {reference} کو {department} کے سپرد کر دیا گیا ہے۔",
        "issue.in_progress": "آپ کی شکایت {reference} پر کام شروع ہو چکا ہے۔",
        "issue.resolved": "شکایت {reference} حل شدہ قرار دی گئی ہے۔ دوبارہ کھولنے کے لیے جواب دیں۔",
        "issue.rejected": "شکایت {reference} بغیر کارروائی بند کر دی گئی: {reason}",
        "issue.duplicate": "شکایت {reference} کو پہلے سے موجود شکایت {parent} میں شامل کر دیا گیا۔",
        "issue.sla_breach": "شکایت {reference} کی {stage} مدت ختم ہو چکی ہے۔",
        "issue.escalated": "شکایت {reference} کو {level} تک بھیج دیا گیا ہے۔",
        "workorder.assigned": "ورک آرڈر {reference} آپ کے سپرد کیا گیا ہے۔",
        "workorder.completed": "ورک آرڈر {reference} مکمل ہو گیا۔",
        "service.approved": "آپ کی درخواست {reference} منظور کر لی گئی ہے۔",
        "service.rejected": "آپ کی درخواست {reference} مسترد کر دی گئی: {reason}",
        "service.info_required": "درخواست {reference} کے لیے مزید معلومات درکار ہیں۔",
        "alert.emergency": "ہنگامی اطلاع: {message}",
        "survey.invite": "{subject} کے بارے میں آپ کی رائے درکار ہے۔",
        "generic.thanks": "اپنے علاقے کی بہتری میں تعاون کا شکریہ۔",
    },
    "sd": {
        "issue.created": "توهان جي شڪايت {reference} وصول ٿي وئي آهي.",
        "issue.assigned": "شڪايت {reference} {department} ڏانهن موڪلي وئي آهي.",
        "issue.resolved": "شڪايت {reference} حل ٿيل قرار ڏني وئي آهي.",
        "generic.thanks": "پنهنجي علائقي جي بهتري ۾ مدد ڪرڻ لاءِ مهرباني.",
    },
    "bal": {
        "issue.created": "شمے شکایت {reference} رسیت.",
        "issue.resolved": "شکایت {reference} حل بوتگ.",
        "generic.thanks": "وتی جاگہ ءَ په بهتر کنگ ءَ منت وار.",
    },
}


def normalise_language(language: str | None) -> str:
    """Map an ``Accept-Language`` fragment onto a supported code."""
    settings = get_settings()
    if not language:
        return settings.default_language
    code = language.split(",")[0].split(";")[0].strip().lower()
    code = code.replace("_", "-")
    if code in LANGUAGES:
        return code
    base = code.split("-")[0]
    if base in LANGUAGES:
        return base
    # Common handset locale aliases that are not ISO codes in their own right.
    alias = {"pa": "ur", "ps": "ur", "bgp": "bal", "bcc": "bal", "snd": "sd"}.get(base)
    if alias and alias in settings.supported_languages:
        return alias
    return settings.default_language


def translate(key: str, language: str | None = None, /, **params: Any) -> str:
    """Look up ``key`` in the catalogue and interpolate ``params``.

    Falls back to English, then to the key itself, so a missing entry degrades
    to something loggable rather than raising in a notification worker.
    """
    lang = normalise_language(language or get_language())
    template = CATALOGUE.get(lang, {}).get(key) or CATALOGUE["en"].get(key) or key
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return template


def direction(language: str | None = None) -> str:
    return LANGUAGES.get(normalise_language(language), LANGUAGES["en"])["direction"]


def available_languages() -> list[dict[str, str]]:
    settings = get_settings()
    return [
        {"code": code, **LANGUAGES[code]}
        for code in settings.supported_languages
        if code in LANGUAGES
    ]
