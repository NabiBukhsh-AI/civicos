"""Translation, moderation and sentiment - the text-hygiene pass.

Every piece of resident-authored text entering the system goes through some
subset of these before it is stored, published or shown to staff:

* **translation** so a report filed in a local language reaches a supervisor who
  reads another one, with the original preserved verbatim,
* **moderation** so personal data never lands on a public complaints board, and
* **sentiment** so the administration can see how people feel, not just what
  they filed.

All three degrade to deterministic rules when no model is configured.
"""

from __future__ import annotations

import structlog

from civicos.ai.prompts import MODERATION_SYSTEM, TRANSLATION_SYSTEM
from civicos.ai.schemas import ModerationResult, SentimentAnalysis, TranslationResult
from civicos.ai.types import CompletionRequest, Message
from civicos.ai.usage import UsageContext, run_completion
from civicos.core.i18n import LANGUAGES, normalise_language
from civicos.core.text import (
    contains_pii,
    guess_language,
    looks_abusive,
    redact_pii,
    truncate,
)

logger = structlog.get_logger(__name__)


async def translate_text(
    text: str,
    target_language: str,
    *,
    source_language: str | None = None,
    usage: UsageContext | None = None,
) -> TranslationResult:
    """Translate ``text`` into ``target_language``."""
    target = normalise_language(target_language)
    detected = source_language or guess_language(text)
    if detected == target:
        return TranslationResult(
            detected_language=detected, translated_text=text, is_translation_needed=False
        )

    target_name = LANGUAGES.get(target, {}).get("name", target)
    request = CompletionRequest(
        messages=[
            Message(
                role="user",
                content=f"Target language: {target_name} ({target})\n\nText:\n{text}",
            )
        ],
        system=TRANSLATION_SYSTEM,
        response_schema=TranslationResult.response_schema(),
        schema_name="translation",
        max_output_tokens=1500,
        temperature=0.0,
    )
    result = await run_completion(request, capability="translation", usage=usage)

    try:
        return TranslationResult.model_validate(result.parsed or {})
    except Exception as exc:
        logger.warning("translation_validation_failed", error=str(exc))
        # Returning the original untouched is the safe failure: a supervisor
        # sees the real words rather than a mistranslation.
        return TranslationResult(
            detected_language=detected, translated_text=text, is_translation_needed=True
        )


async def moderate_text(
    text: str, *, usage: UsageContext | None = None, use_model: bool = True
) -> ModerationResult:
    """Screen resident text before publication.

    The regex pass always runs; the model pass adds judgement about abuse and
    scope. Regex findings are never overridden by the model - a phone number
    the pattern caught stays redacted regardless of what the model thinks.
    """
    rule_based = _rule_based_moderation(text)
    if not use_model:
        return rule_based

    request = CompletionRequest(
        messages=[Message(role="user", content=truncate(text, 4000))],
        system=MODERATION_SYSTEM,
        response_schema=ModerationResult.response_schema(),
        schema_name="moderation",
        max_output_tokens=1200,
        temperature=0.0,
    )
    result = await run_completion(request, capability="moderation", usage=usage)

    try:
        model_result = ModerationResult.model_validate(result.parsed or {})
    except Exception:
        return rule_based

    return ModerationResult(
        is_acceptable=model_result.is_acceptable and rule_based.is_acceptable,
        contains_personal_data=(
            model_result.contains_personal_data or rule_based.contains_personal_data
        ),
        is_abusive=model_result.is_abusive or rule_based.is_abusive,
        is_spam=model_result.is_spam,
        is_out_of_scope=model_result.is_out_of_scope,
        # Always re-run the deterministic redactor over the model's output.
        redacted_text=redact_pii(model_result.redacted_text or text),
        reason=model_result.reason or rule_based.reason,
    )


def _rule_based_moderation(text: str) -> ModerationResult:
    has_pii = contains_pii(text)
    abusive = looks_abusive(text)
    return ModerationResult(
        is_acceptable=not abusive,
        contains_personal_data=has_pii,
        is_abusive=abusive,
        is_spam=False,
        is_out_of_scope=False,
        redacted_text=redact_pii(text),
        reason="Contains abusive language." if abusive else None,
    )


async def analyse_sentiment(
    text: str, *, usage: UsageContext | None = None
) -> SentimentAnalysis:
    """Classify feedback sentiment and pull out recurring themes."""
    request = CompletionRequest(
        messages=[
            Message(
                role="user",
                content=(
                    "Classify the sentiment of this resident feedback and list the "
                    f"themes it raises.\n\n{truncate(text, 3000)}"
                ),
            )
        ],
        system=(
            "You analyse resident feedback for a municipal administration. Be "
            "factual and neutral. Themes are short noun phrases such as "
            "'waste collection delays' or 'staff responsiveness'."
        ),
        response_schema=SentimentAnalysis.response_schema(),
        schema_name="sentiment",
        max_output_tokens=600,
        temperature=0.0,
    )
    result = await run_completion(request, capability="summary", usage=usage)

    try:
        return SentimentAnalysis.model_validate(result.parsed or {})
    except Exception:
        from civicos.ai.providers.heuristic import _sentiment  # noqa: PLC0415

        label = _sentiment(text)
        return SentimentAnalysis(
            sentiment=label,  # type: ignore[arg-type]
            score={"positive": 0.5, "negative": -0.5, "mixed": 0.0, "neutral": 0.0}[label],
            themes=[],
            actionable_suggestion=None,
        )
