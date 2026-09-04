"""Tests for the AI layer's contracts, fallbacks and guardrails.

These exercise the properties that make the AI safe to deploy in a government
service: structured output is enforced, the offline path always works, model
output is validated before it is trusted, and answers cannot cite sources that
were never retrieved.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from civicos.ai.pricing import estimate_cost_usd, rate_for
from civicos.ai.providers.base import coerce_schema_defaults, extract_json
from civicos.ai.providers.heuristic import HeuristicProvider, classify, hashing_embedding
from civicos.ai.rag.chunking import chunk_text, estimate_tokens
from civicos.ai.schemas import (
    ExecutiveBriefing,
    GroundedAnswer,
    ModerationResult,
    TriageResult,
    VisionReport,
)
from civicos.ai.types import CompletionRequest, ImagePart, Message
from civicos.core.text import cosine_similarity


class TestSchemas:
    @pytest.mark.parametrize(
        "model", [TriageResult, VisionReport, GroundedAnswer, ModerationResult, ExecutiveBriefing]
    )
    def test_schemas_meet_strict_mode_requirements(self, model) -> None:
        """Providers that enforce schemas require these invariants."""
        schema = model.response_schema()
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert "$defs" not in schema, "nested definitions must be inlined"

    def test_nested_models_are_inlined(self) -> None:
        schema = VisionReport.response_schema()
        findings = schema["properties"]["findings"]["items"]
        assert findings["additionalProperties"] is False
        assert "label" in findings["properties"]

    def test_schema_rejects_unknown_keys(self) -> None:
        with pytest.raises(PydanticValidationError):
            TriageResult.model_validate(
                {
                    "category_slug": "roads",
                    "confidence": 0.9,
                    "priority": "normal",
                    "severity": "minor",
                    "title": "t",
                    "summary": "s",
                    "suggested_department_slug": None,
                    "is_emergency": False,
                    "requires_field_visit": False,
                    "tags": [],
                    "reasoning": "r",
                    "missing_information": [],
                    "unexpected": "value",
                }
            )


class TestJSONRecovery:
    def test_plain_json(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json_block(self) -> None:
        assert extract_json('Here you go:\n```json\n{"a": 2}\n```\nHope that helps.') == {"a": 2}

    def test_json_embedded_in_prose(self) -> None:
        assert extract_json('The answer is {"a": 3} as requested.') == {"a": 3}

    def test_unrecoverable_returns_none(self) -> None:
        assert extract_json("no json at all here") is None
        assert extract_json("") is None

    def test_missing_required_keys_are_filled(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "array"}},
            "required": ["a", "b"],
        }
        assert coerce_schema_defaults({"a": "x"}, schema) == {"a": "x", "b": []}


class TestHeuristicProvider:
    """The offline provider is the platform's floor - it must always work."""

    async def test_structured_output_satisfies_the_schema(self) -> None:
        provider = HeuristicProvider(dimensions=64)
        result = await provider.complete(
            CompletionRequest(
                messages=[Message(role="user", content="The drain is blocked and overflowing.")],
                response_schema=TriageResult.response_schema(),
                context={"classify_text": "The drain is blocked and overflowing."},
            )
        )
        assert result.parsed is not None
        TriageResult.model_validate(result.parsed)  # must not raise

    async def test_vision_output_satisfies_the_schema(self) -> None:
        provider = HeuristicProvider(dimensions=64)
        result = await provider.complete(
            CompletionRequest(
                messages=[
                    Message(
                        role="user",
                        content="Assess this site.",
                        images=[ImagePart(data=b"\xff\xd8\xff", media_type="image/jpeg")],
                    )
                ],
                response_schema=VisionReport.response_schema(),
            )
        )
        VisionReport.model_validate(result.parsed)

    async def test_prose_output_is_honest_about_being_offline(self) -> None:
        provider = HeuristicProvider(dimensions=64)
        result = await provider.complete(
            CompletionRequest(messages=[Message(role="user", content="Tell me about fees.")])
        )
        assert "not configured" in result.text.lower()

    def test_keyword_rules_classify_common_reports(self) -> None:
        assert classify("garbage piled up on the street")[0] == "solid-waste"
        assert classify("the streetlight is not working")[0] == "streetlights"
        assert classify("sewage overflowing from the manhole")[0] == "sewerage"
        assert classify("a pothole in the road")[0] == "roads"

    def test_hazards_are_treated_as_emergencies(self) -> None:
        _, priority, severity, _ = classify("the wire is sparking above the footpath")
        assert priority == "emergency"
        assert severity == "critical"

    def test_urgency_language_raises_priority(self) -> None:
        _, normal, _, _ = classify("garbage on the street")
        _, urgent, _, _ = classify("garbage on the street, urgent, next to a school")
        assert urgent != normal

    def test_unmatched_text_falls_back_without_pretending(self) -> None:
        slug, _, _, matched = classify("something entirely unrelated to municipal work")
        assert slug == "other"
        assert matched is False


class TestHashingEmbeddings:
    def test_near_duplicates_score_higher_than_unrelated_text(self) -> None:
        a = hashing_embedding("garbage not collected in street 5 for two weeks", 512)
        b = hashing_embedding("street 5 garbage uncollected for two weeks", 512)
        c = hashing_embedding("the streetlight near the park is broken", 512)
        assert cosine_similarity(a, b) > cosine_similarity(a, c)
        assert cosine_similarity(a, b) > 0.4

    def test_embeddings_are_deterministic(self) -> None:
        text = "blocked drain on the main road"
        assert hashing_embedding(text, 128) == hashing_embedding(text, 128)

    def test_identical_text_is_maximally_similar(self) -> None:
        vector = hashing_embedding("a blocked drain", 128)
        assert cosine_similarity(vector, vector) == pytest.approx(1.0)

    def test_empty_text_is_handled(self) -> None:
        assert hashing_embedding("", 32) == [0.0] * 32

    def test_dimension_is_respected(self) -> None:
        assert len(hashing_embedding("anything", 256)) == 256


class TestChunking:
    def test_short_text_is_one_chunk(self) -> None:
        chunks = chunk_text("A short municipal notice.")
        assert len(chunks) == 1

    def test_long_text_is_split_with_indexes(self) -> None:
        chunks = chunk_text("This is a clause about waste management. " * 200)
        assert len(chunks) > 1
        assert [chunk.index for chunk in chunks] == list(range(len(chunks)))

    def test_headings_become_section_titles(self) -> None:
        document = (
            "SECTION 1 GENERAL PROVISIONS\n"
            + ("A clause about the collection of waste. " * 40)
            + "\nSCHEDULE II: Fees\n"
            + ("The prescribed fee is as follows. " * 30)
        )
        chunks = chunk_text(document)
        titles = {chunk.section_title for chunk in chunks}
        assert "SECTION 1 GENERAL PROVISIONS" in titles
        assert "SCHEDULE II: Fees" in titles

    def test_empty_input_yields_nothing(self) -> None:
        assert chunk_text("") == []
        assert chunk_text("   \n  ") == []

    def test_chunks_respect_the_size_budget(self) -> None:
        chunks = chunk_text("word " * 5000, chunk_size=500, overlap=50)
        # Sentence-aware packing can overshoot slightly; never wildly.
        assert all(len(chunk.content) <= 800 for chunk in chunks)

    def test_token_estimate_is_monotonic(self) -> None:
        assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)


class TestPricing:
    def test_known_models_are_priced(self) -> None:
        assert rate_for("claude-opus-5").input_per_million == 5.00
        assert rate_for("claude-sonnet-5").output_per_million == 10.00

    def test_dated_snapshots_inherit_the_family_rate(self) -> None:
        assert rate_for("claude-opus-5-20260101") == rate_for("claude-opus-5")

    def test_unknown_models_are_not_free(self) -> None:
        """Reporting zero cost for an unrecognised model would hide real spend."""
        assert estimate_cost_usd("some-new-model", 1_000_000, 0) > 0

    def test_offline_provider_costs_nothing(self) -> None:
        assert estimate_cost_usd("heuristic-v1", 1_000_000, 1_000_000) == 0.0

    def test_cached_tokens_are_discounted(self) -> None:
        full = estimate_cost_usd("claude-opus-5", 100_000, 0)
        cached = estimate_cost_usd("claude-opus-5", 100_000, 0, cached_input_tokens=100_000)
        assert cached < full


class TestGuardrails:
    def test_prompts_forbid_invention_and_promises(self) -> None:
        from civicos.ai.prompts import ASSISTANT_SYSTEM, BASE_GUARDRAILS, TRIAGE_SYSTEM

        for prompt in (BASE_GUARDRAILS, TRIAGE_SYSTEM, ASSISTANT_SYSTEM):
            lowered = prompt.lower()
            assert "never invent" in lowered
            assert "never promise" in lowered

    def test_taxonomy_block_renders_tenant_categories(self) -> None:
        from civicos.ai.prompts import taxonomy_block

        rendered = taxonomy_block(
            [{"slug": "drainage", "name": "Drainage", "keywords": ["nala", "drain"]}]
        )
        assert "drainage" in rendered
        assert "nala" in rendered

    def test_empty_taxonomy_is_handled(self) -> None:
        from civicos.ai.prompts import taxonomy_block

        assert "other" in taxonomy_block([])

    def test_context_block_labels_sources_for_citation(self) -> None:
        from civicos.ai.prompts import context_block

        rendered = context_block(
            [{"title": "Waste Bylaw", "page": 4, "document_id": "abc", "content": "text"}]
        )
        assert "[1]" in rendered
        assert "document_id=abc" in rendered
        assert "page 4" in rendered
