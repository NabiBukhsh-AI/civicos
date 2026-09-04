# AI design

The premise: an AI system inside a government service is accountable to
residents. It has to be auditable, degradable, budgeted and honest about what
it does not know. Those four properties drove every decision below.

---

## 1. AI is advisory, never authoritative

What the model proposed and what the municipality decided are stored in
different columns:

| Model's view | Municipality's decision |
|---|---|
| `ai_category_slug` | `category_id` |
| `ai_priority` | `priority` |
| `ai_severity` | `severity` |
| `ai_confidence`, `ai_summary`, `ai_analysis`, `ai_model`, `ai_triaged_at` | the workflow columns |

A supervisor can always see that the model said "Sanitation / high" and that a
human moved it. Every triage writes an `AI_TRIAGED` timeline event recording
whether its suggestion was applied and at what confidence — including when it
was *not* applied, which is the more interesting case.

Below the tenant's confidence threshold (`ai_triage_confidence`, default 0.5)
the suggestion is recorded but not acted on, and the report lands in a manual
triage queue. A vague report *should* reach a human.

---

## 2. The platform works without AI

`HeuristicProvider` is not a stub. It is the floor the whole system stands on,
and it serves two purposes: the test suite runs with no key and no network,
and a municipality that has not procured an AI vendor still gets working
triage, search and deduplication.

| Capability | With a model | Offline |
|---|---|---|
| Triage | Full classification with reasoning | Keyword rules over a curated taxonomy, urgency detection, hazard escalation |
| Embeddings | Vendor embeddings | Hashing trick over word unigrams, bigrams and character 4-grams |
| Duplicates | Model adjudication of borderline pairs | Geo + lexical + hashing-embedding similarity |
| Assistant | Grounded answer with citations | Honest "AI is not configured on this deployment" |
| Vision | Structured site assessment | Files stored, EXIF extracted, manual review flagged |

The hashing embeddings are genuinely useful, not a placeholder: "garbage not
collected in street 5" and "street 5 garbage uncollected" land at cosine ≈0.56
while an unrelated streetlight report sits at ≈0.16. That separation is enough
to catch real duplicates. Because the offline threshold differs from a model
threshold, `duplicate_service` selects the right one automatically.

Degradation is also the budget behaviour: when a tenant exhausts
`daily_token_budget`, calls fall back to the offline provider instead of
returning errors. Intake never stops.

---

## 3. Structured output, validated twice

Every capability declares a Pydantic model in `ai/schemas.py` and hands its
JSON Schema to the provider. `_strictify` inlines `$defs`, forces
`additionalProperties: false` and lists every property in `required`, which is
what schema-enforcing providers expect.

The response is then validated again on our side. A response that fails
validation does not propagate — each capability has a deterministic fallback:

```python
try:
    return TriageResult.model_validate(result.parsed or {})
except Exception:
    return _fallback(title, description, valid_slugs)   # rule-based
```

Hallucinated category slugs are caught explicitly: a slug outside the tenant's
taxonomy is rewritten to `other` with reduced confidence and a note saying what
the model proposed. A phantom category is never created.

---

## 4. Grounding is enforced, not requested

`GroundedAnswer` carries `answered_from_context`. When the retrieved passages
do not support an answer, the model sets it false, the API reports
`grounded: false` and `escalate_to_human: true`, and the resident is told which
office to contact instead. A short honest answer beats a confident invented one
— particularly when the invented one is a fee schedule.

Citations are reconciled against what was actually retrieved
(`_reconcile_citations`). A citation naming a document the retriever never
returned is dropped. Publishing a fabricated source to a resident is the
failure mode that matters most here.

Retrieval is permission-aware. `AUDIENCE_VISIBILITY` maps an actor to the
visibility levels they may retrieve from, so an internal SOP cannot surface in
a citizen-facing answer even though it lives in the same index.

---

## 5. Prompts are versioned policy

`ai/prompts.py` holds every prompt in one module with a `PROMPT_VERSION`.
Prompts are reviewable like any other municipal policy document, and the
version is recorded so "what changed the day triage accuracy dropped" is
answerable.

`BASE_GUARDRAILS` applies to every citizen-facing generation:

- never invent facts, fees, deadlines, laws, phone numbers or officials' names
- never promise a repair, a timeline, an approval or a payment
- no legal, medical or financial advice
- no personal data about any individual
- politically neutral: no commentary, no comparisons between officials
- immediate danger → say emergency services must be contacted first

Capability prompts add specifics. The vision prompt forbids identifying people
or reading number plates. The moderation prompt states that criticism of the
administration is not abuse. The duplicate prompt says that when unsure, answer
false — because a wrong merge hides a resident's report.

The tenant's taxonomy is injected as *data* (`taxonomy_block`), never baked
into the prompt, so a town's own vocabulary takes effect immediately.

---

## 6. Everything is metered

All model calls go through `run_completion` / `run_embedding`, which:

1. check the tenant's daily token budget,
2. dispatch to the provider for that capability,
3. fall back to the offline provider on failure,
4. record Prometheus counters, and
5. append a row to `ai_usage` — capability, provider, model, user, department,
   tokens, latency, estimated cost, success, and the entity it related to.

Cost comes from an editable table in `ai/pricing.py`. Prefix matching means
dated snapshots inherit their family's rate, and an unknown model falls back to
a conservative estimate rather than reporting zero — silently under-reporting
spend would be worse than being slightly wrong.

`GET /api/v1/assistant/usage` answers "what did the AI cost this month, by
capability" without guesswork.

---

## 7. Provider-agnostic by construction

```python
class LLMProvider(ABC):
    name: str
    supports_vision: bool
    async def complete(self, request: CompletionRequest) -> CompletionResult
    async def embed(self, texts: list[str], model: str | None) -> EmbeddingResult
```

A provider owns transport only — auth, retries, response shape. No prompts, no
business rules, no persistence. That boundary is what lets the whole AI layer
be tested without a network.

Shipped: `anthropic` (Messages API, native `output_config.format` schema
enforcement, refusal handling), `google` (google-genai, native response
schemas, embeddings), `openai` (also covers OpenAI-compatible gateways such as
vLLM or a local model server via `base_url` — useful under data-residency
constraints), and `heuristic`.

Capabilities route independently:

```bash
CIVICOS_AI__PROVIDER=anthropic            # chat and triage
CIVICOS_AI__VISION_PROVIDER=google        # vision
CIVICOS_AI__EMBEDDING_PROVIDER=openai     # embeddings
```

Adding a vendor:

```python
from civicos.ai.registry import register_provider
register_provider("local", lambda: MyProvider(base_url="http://vllm:8000"))
```

Nothing above the provider layer changes.

---

## 8. Retrieval

**Chunking** (`ai/rag/chunking.py`) is structure-aware. Municipal documents are
mostly scanned bylaws, budget tables and notifications, so the chunker
recognises headings (`SECTION 4`, `CHAPTER II — Waste`, `4.2 Collection
points`, `GENERAL PROVISIONS`), tracks page numbers through extraction so
citations can name a page, and falls back to sentence-aware packing with
overlap. A bylaw clause cut in half produces a citation that is worse than
useless.

**Storage** is a `Vector` column: real `vector(N)` under pgvector, JSON
elsewhere. `get_vector_store` picks the backend by inspecting the connection.

**Search** is hybrid. Semantic similarity carries meaning; lexical overlap
rescues exact terms — a scheme number, a section reference — that embeddings
routinely blur. Weighting is tunable (`CIVICOS_RAG__KEYWORD_WEIGHT`).

**Selection** over-fetches `top_k × candidate_multiplier`, then packs to a
character budget while limiting how many chunks come from any one document.
Municipal corpora repeat themselves heavily — the same clause appears in a
bylaw, its amendment and the summary circular — so diversity gives the model
three angles instead of the same paragraph three times.

---

## 9. Vision as evidence

`VisionReport` returns a category, severity, condition, hazards, recommended
actions, a crew-hours estimate and a confidence — not a paragraph.
`image_quality_note` exists because blur and darkness are the normal case for
photographs taken at night beside a drain, and a model that does not say so is
overconfident.

EXIF handling sits alongside: GPS converted exactly from rationals and
validated, capture time parsed with the recorded UTC offset applied. Two things
follow:

- **Location promotion** — when a report has no coordinates but its photo does,
  the photo's location is used. A resident who photographed the problem has
  already told us where it is; making them drop a pin as well loses reports.
- **Consistency check** — when both exist and disagree beyond tolerance, the
  report is flagged for a human. Never auto-rejected: phones lie, EXIF gets
  stripped, and people photograph a blocked drain from across the road.

`compare_before_after` is deterministic, not a second model call, and only
flags a completion for supervisor review.

---

## 10. Privacy

Text is redacted before it reaches any model or any public surface. Phone
numbers, national ID numbers and email addresses are stripped by regex in
`core/text.redact_pii`, and the deterministic redactor is re-applied over the
model's own output — a pattern match is never overridden by model judgement.

The verbatim original is retained in `description_original` for the case file.
Residents put their phone number in the description constantly; the answer is
to keep it out of the public record, not to lose it.

---

## Testing the AI layer

`tests/unit/test_ai.py` asserts the properties, not the prose: strict-schema
invariants for every output model, JSON recovery from fenced and embedded
output, offline-provider output validity against the real schemas, embedding
separation between near-duplicates and unrelated text, chunker heading
detection, pricing behaviour for unknown models, and that the guardrail
language is actually present in the prompts.

None of it needs a network or a key.
