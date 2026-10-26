# Data Contracts

This document records the data contracts for GeoSpoiler enriched v2 pipeline.
All cards use `enriched_v2` schema. No backward compatibility with v1.

New normalized sidecars include code-owned `normalized_text_sha256`, calculated
over the saved UTF-8 text bytes. Text and sidecar are staged separately and the
sidecar is published last. Enrichment verifies the digest before admission and
again before processing, so an interrupted overwrite cannot pair new text with
old metadata. Existing sidecars without this additive field retain their schema
validation path. A missing, empty or mismatched pair cannot publish a new card.
Overwriting an existing pair uses the established durable rollback journal,
including for legacy sidecars without a digest. Readers recover an interrupted
publication before validating input, preserving either the old complete pair
or the newly committed pair.

Selective-media sidecars retain `attachment_id`, media type, message ID and post
URL. Code owns `analysis_operations`, `selection_reason` and `analysis_status`:
`not_analyzed` is a policy decision, while `success` and `failed` describe selected
operations. Existing download/transcription/visual statuses and error diagnostics
remain available. Unselected operations do not generate missing-analysis errors
or claims that unseen media is unimportant.

## Modules

- `src/geospoiler/contracts/models.py` — Pydantic models: `LLMPayload`, `EnrichedCardV2`, `NormalizedMeta`
- `src/geospoiler/ingestion/enricher/card_validation.py` — validators and Markdown report generation
- `src/geospoiler/ingestion/enricher/validator.py` — runtime contract checks on LLM output
- `src/geospoiler/ingestion/enricher/preprocessor.py` — text cleaning before LLM
- `src/geospoiler/ingestion/enricher/repair.py` — single repair pass for contract violations

## Pipeline Architecture

```
normalized text + .meta.json
  -> content_type classifier (rule-based)
  -> triage (keep or reviewer queue)
  -> preprocessor (header, clean_text, ignored_blocks)
  -> LLM extraction (returns LLMPayload only)
  -> structural schema validation
  -> validator (semantic contract checks)
  -> repair (optional, max 1 LLM call total per card)
  -> postprocessor (assembles EnrichedCardV2)
  -> graph_text / search_text builders (code-built)
  -> enriched_v2 card written to disk
```

## Responsibility Split

### LLM returns (LLMPayload):
- summary, key_points, entities, topics, theses, quotes, events, search_phrases, quality_flags

### Code builds:
- schema_version, prompt_version, enrichment_model, enriched_at
- provenance, content_type, language
- source_chain, ignored_blocks
- graph_text, search_text

## EnrichedCardV2 Fields

- `schema_version` — always `"enriched_v2"`
- `prompt_version` — version of the extraction instructions, e.g. `"enriched_prompt_v2"`
- `enrichment_model` — model used for extraction
- `enriched_at` — ISO timestamp
- `provenance` — `{source_id, source_type, channel, date, post_url, message_id, forwarded_from, normalized_path}`
- `content_type` — one of: `telegram_post`, `telegram_forward`, `youtube_transcript`, `instagram_text`, `web_article_text`, `mixed_normalized_text`, `unknown`
- `language` — default `"ru"`
- `summary` — 1-3 sentences in Russian, source-attributed
- `key_points` — list of `{text, type, importance, evidence}`
- `entities` — dict of categories, each a list of `{text, role, salience}`
- `topics` — list of `{label, salience, type}`
- `theses` — list of `{text, speaker, stance, evidence}`
- `quotes` — list of `{text, speaker, context}`
- `events` — list of `{event_type, description, date_text, date_normalized, location, actors}`
- `search_phrases` — list of `{text, source}`
- `source_chain` — `{original_source, forwarded_from, mentioned_sources, external_links}`
- `graph_text` — code-built compact relational text for LightRAG
- `search_text` — code-built dense flat text for FTS/BM25
- `ignored_blocks` — preprocessor-detected media placeholders `{type, text}`
- `quality_flags` — data quality signals (list of strings)
- `extraction_issues` — validator diagnostics retained when a repair could not
  remove a contract violation; excluded from graph and search text

## Removed from v2 (no longer exist)

- `key_facts` / `claim_type`
- `query_aliases`
- `triage` / `triage_reason`
- `visual` / `broll`
- `noise`
- `dedup`
- `version` (int)
- `chunks` (long inputs are chunked and merged internally, but chunks are not persisted)

Long-text chunk extraction carries every LLM-owned semantic field through the
internal merge. `ignored_blocks` never comes from chunk LLM output: the
preprocessor detects it once for the complete document and the postprocessor
copies it to the final card.

## Key Point Types

Allowed `key_points[].type`:

- `reported_statement`
- `reported_event`
- `opinion`
- `prediction`
- `accusation`
- `quote_summary`
- `source_reference`
- `announcement`
- `numeric_claim`
- `other`

## Content Types (rule-based, code determines)

- `telegram_post`
- `telegram_forward`
- `youtube_transcript`
- `instagram_text`
- `web_article_text`
- `mixed_normalized_text`
- `unknown`

## Entity Structure

```json
{"text": "NATO", "role": "organization", "salience": "primary"}
```

Categories: people, organizations, countries, locations, military_units, equipment, weapons, programs_projects, media_sources, other.

No `evidence` field in entities for MVP.

## Strict Enumerations

- `key_points[].importance`: `high`, `medium`, `low`
- `entities.*[].salience` and `topics[].salience`: `primary`, `secondary`, `mentioned`
- `topics[].type`: `case_topic`, `policy_topic`, `military_topic`, `diplomatic_topic`, `economic_topic`, `rhetoric_topic`, `source_topic`, `regional_topic`, `technology_topic`, `sanctions_topic`, `energy_topic`, `migration_topic`, `other`
- `theses[].stance`: `supportive`, `critical`, `accusatory`, `alarmist`, `sarcastic`, `neutral_explanatory`, `interpretive`, `predictive`, `mobilizing`, `unclear`
- `events[].event_type`: `reported_statement`, `meeting`, `agreement`, `attack`, `strike`, `military_movement`, `exercise`, `launch`, `decision`, `vote`, `publication`, `announcement`, `negotiation`, `sanction`, `accusation`, `arrest`, `border_incident`, `economic_measure`, `unknown`
- `search_phrases[].source`: `surface_form`, `phrase_from_text`, `constructed_from_present_terms`
- `ignored_blocks[].type`: `image`, `video`, `audio`, `instagram`, `ai_chat`, `media_omitted`, `unknown`
- `quality_flags`: `mostly_boilerplate`, `very_short_text`, `contains_legacy_media_placeholders`, `no_substantive_content`, `unclear_source_chain`, `mixed_topics`, `possible_duplicate`, `extraction_unstable`

Unknown fields and values are schema errors. Raw LLM output is validated against
`LLMPayload` before it is accepted; legacy `query_aliases` is rejected.

## Extraction Failure Policy

- A substantive input must not turn an empty LLM response into an empty card.
- Empty, non-object, or schema-invalid raw JSON is a structural extraction failure.
- Structural failure may use the job's single repair LLM call. If that repair is
  empty or still schema-invalid, the job fails explicitly. No card is written and
  enrichment progress is not updated; the batch continues with the next job.
- Structural and semantic repair share one budget: at most one repair LLM call per job.
- If a structurally valid payload violates semantic contract rules, an unsuccessful
  repair may be persisted with `extraction_unstable` only when `summary` or
  `key_points` still contains usable extracted content.
- A substantive semantic payload with neither `summary` nor `key_points` is an
  unusable extraction and fails without writing output or progress.

## Language Policy

- summary, key_points, topics, theses, events, graph_text — Russian
- entities.text — original surface form (NATO, DPRK, Xi Jinping)
- quotes.text — original language (verbatim)
- search_text — mixed (Russian + original terms)
- search_phrases — mixed
- The validator rejects Ukrainian-specific characters and clearly dominant
  non-Russian Latin or Han prose in required Russian semantic fields. Exact
  entity surface forms are excluded from this language decision.
- A language violation consumes the same single repair attempt as other semantic
  violations. If it remains after repair, the job fails and neither a card nor
  an enrichment-progress record is published.
- Verbatim quotes are included in `search_text`, but not in `graph_text`; this
  preserves multilingual retrieval without introducing foreign-language prose
  into the graph extraction input.

## Wiki Contract Status

The active Wiki contract is SQLite schema v6,
`geospoiler-wiki-sqlite-v6-effective-relations-review-fts-analysis-cache`.
Existing databases with another version/contract are rejected; there is no
silent migration or reset.

The contract is retained for future use, but the subsystem is dormant by
default. `WIKI_ENABLED=false` prevents configured ingest, review, projection,
CLI, and retrieval paths from opening or mutating Wiki state.

Source-of-truth classes:

- Enriched v2 and YouTube segment revisions;
- approved concepts and identity aliases;
- concept/hierarchy review history, including rejects and reopens;
- pinned claim-specific ambiguity and grouping/link overrides;
- manual sidecar Markdown.

Rebuildable classes:

- immutable extraction artifacts and effective occurrence lifecycle;
- registry candidates and surface snapshots;
- cached Luna identity/hierarchy analyses keyed by their exact inputs;
- exact claim groups and automatic concept links;
- computed card relations;
- card, claim, and hub projections;
- generated Markdown and Wiki FTS documents.

Identity invariants:

- one lineage comes from native `provenance.source_id` or `segment_id`, never
  from a file path/array index;
- `occurrence_version_id` uses lineage, field kind, stable locator, exact
  payload hash, and occurrence schema version; a card-wide hash is provenance,
  not occurrence identity;
- current state is selected by committed run sequence/generation, never by
  timestamp;
- exact locator continuity may produce `superseded`; otherwise changed text is
  `retired` plus a new active occurrence;
- claim occurrences are never physically merged;
- authoritative overrides overlay automatic results and become `stale` rather
  than disappearing when their fingerprint no longer matches.

Registry/hub invariants:

- proposals are not concepts;
- a surface needs two independent substantive clusters; reposts do not satisfy
  that threshold;
- identity aliases are `canonical`, `technical`, `abbreviation`,
  `translation`, or `spelling`; contextual metonyms are forbidden aliases;
- relation roles are `subject | actor | object | context | mentioned |
  unknown`, with `unknown` safely aggregating as context;
- one card may relate to multiple approved concepts;
- one primary hierarchy parent is allowed; related concepts are bounded;
- Markdown hubs contain references to Enriched cards, not copied card state;
- `manual_sidecars` are authoritative, while `projection_artifacts`,
  `data/wiki/projections/`, and `wiki_fts_documents` are disposable.

The complete table, CAS, extraction, and projection contract is documented in
`docs/wiki_v2_sqlite_contract.md`.

## Validator Rules

Contract checks (not truth):
- No `canonical_id`
- No aliases absent from text
- Quotes present in source text
- Topics not overly generic
- Entities not from ignored blocks
- No forbidden quality_flags (fake_news, propaganda, false_claim)
- Payload not empty when text is substantive
- `no_substantive_content` is removed when the final payload contains content
- `extraction_unstable` is assigned by the pipeline only after validation/repair
  failure; the remaining validator violations are stored in `extraction_issues`

## Provenance

Stable source ids derived in order:
1. Existing `source_id`
2. `telegram:{channel_id}:{message_id}`
3. `telegram:{channel}:{message_id}`

Every persisted v2 card must contain a non-empty `provenance.source_id`.
Progress invalidation includes the SHA-256 fingerprint of both normalized text and
metadata, schema version, prompt version, and enrichment model. A metadata-only
change therefore cannot leave an old enriched card marked up to date.

## YouTube Long-Form Checkpoints

- Long YouTube sources are segmented when they cross the deterministic character
  or duration threshold in `src/geospoiler/ingestion/enricher/youtube_segmenter.py`.
- Each successfully extracted segment is written to an isolated checkpoint under
  `data/state/youtube_checkpoints/<source-digest>/<fingerprint>/` before the next
  segment is requested.
- The checkpoint manifest records the checkpoint fingerprint, active
  `enrichment_model`, expected segment count, file names, and SHA-256 hashes. A
  `youtube_segment_v2` card also records `enrichment_model`.
- The source fingerprint controls whether a completed episode card is current.
  The separate checkpoint fingerprint additionally includes the active model.
  Therefore changing profile can leave a completed card untouched while never
  reusing an in-progress segment from another model.
- A changed transcript, metadata, model, prompt, schema, or segmentation version
  creates a different checkpoint fingerprint and cannot reuse the old checkpoint.
- Checkpoints are removed only after the episode card, manifest, and child
  segments are successfully published. An interrupted run can therefore resume
  completed segments without publishing a half-written final generation.
- Final-generation cleanup of old backups and legacy outputs is best-effort and
  happens after the new generation is committed; cleanup failure must not roll
  back a valid new generation.

## Validation Command

```powershell
python main.py validate enriched
```

Scans `data/enriched`, writes a Markdown report under `artifacts/`.

## Physical relocation without a schema change

The structure migration preserves card bytes, source IDs, graph_text and document
identity. Stored `output/...`/old absolute paths remain logical addresses;
`geospoiler.paths.resolve_project_path` resolves them to active files under data.
Callers must apply their existing allowed-root and symlink checks to the resolved
physical path. Do not rewrite a saved generation just to update its directory.
The data-layout marker retains the identity root used by document IDs and YouTube
fingerprints. This mapping is separate from the enriched_v2 schema version.
