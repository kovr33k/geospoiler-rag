# Wiki v2 SQLite Contract — Schema v6 (Approved Hubs and Effective Relations)

Status: frozen, implemented, and connected to runtime in `retrieval.wiki`.

The contract covers deterministic Enriched/YouTube ingest, immutable occurrence
lifecycle, eligibility, reviewed registry/aliases, exact reversible grouping,
role-aware concept links, claim-local metonyms, reviewed hierarchy, manual
sidecars, disposable projections, FTS, CLI, hybrid retrieval, and the browser
reviewer.

## Source of truth

Authoritative state must survive every rebuild:

- Enriched v2 card revisions and their source lineage;
- approved concepts and identity aliases;
- processor-contract activation history;
- concept review decisions and rejects/reopens;
- claim/link/metonym overrides, including stale decisions;
- approved primary hierarchy and related-topic decisions;
- manual sidecar Markdown.

Derived state may be rebuilt from authoritative state and immutable inputs:

- input projections and stage snapshots;
- extraction artifacts and claim occurrences;
- automatic claim grouping and occurrence-to-concept links;
- surface/candidate snapshots;
- computed card relations and their contributors;
- hierarchy proposals;
- generated card/claim/hub projection artifacts;
- Wiki FTS documents;
- successful Luna identity/hierarchy responses cached by exact input,
  prompt, and model profile.

SQLite stores both classes. Their ownership, producer, immutable history, and
effective overlay must remain distinguishable.

## Identity and ordering

- Native lineages, card revisions, extraction artifacts, and occurrence
  versions use deterministic namespaced SHA-256 IDs.
- Internal state versions and attempt IDs may remain opaque UUID strings.
- Audit timestamps never choose a winner.
- Per-head generations detect `A → B → A`, even when the final hash equals A.
- Successfully applied stage runs receive a unique `commit_seq`.
- Current occurrence state is ordered by the `commit_seq` of committed runs.
- A version row is never reused as a new head generation.

## Schema and connection contract

`schema_metadata` contains one row for schema version `6` and contract
`geospoiler-wiki-sqlite-v6-effective-relations-review-fts-analysis-cache`.
Initialization of a fresh v6 database is idempotent. Any other version,
including every development v1/v2/v3/v4/v5 shape, is rejected before DDL runs.
The same version with a different contract string is also rejected before
schema mutation. There is no silent repair, reset, or migration.

Every connection uses:

```text
PRAGMA foreign_keys = ON
PRAGMA busy_timeout = configured milliseconds
PRAGMA synchronous = NORMAL
PRAGMA journal_mode = WAL      # file databases where supported
```

All head changes and stage applies use short `BEGIN IMMEDIATE` transactions.

## Table groups

### Authoritative ingest and input revisions

| Table | Purpose |
|---|---|
| `source_lineages` | Stable source identity. |
| `card_revisions` | Immutable canonical Enriched-card payload and content hash. |
| `source_lineage_heads` | Current full card revision; diagnostic for stage CAS. |
| `lineage_input_versions` | Immutable version of one named input projection. |
| `lineage_input_heads` | Current generation/hash for each lineage and input kind. |
| `card_revision_input_bindings` | Exact input version first associated with a card revision. |

Input kinds overlap. A field may contribute to any number of input hashes. The
ingest controller recalculates every applicable hash and advances only those
whose value changed. Examples:

```text
summary       → card_projection_inputs
key_point     → claim_inputs + card_projection_inputs
topic/entity  → structured_relation_inputs (+ projection where displayed)
quality flag  → eligibility_inputs
```

Changing only `summary` therefore cannot stale claim extraction.

### User-facing Enriched v2 ingest contract

The offline adapter accepts validated `EnrichedCardV2` and
`YouTubeSegmentCardV2` objects, a JSON file, or a recursively scanned directory.
It performs no network or LLM calls. A whole JSON document is validated before
any card from that document is recorded; an invalid file cannot partially
create Wiki state and does not stop later files.

Directory traversal is deterministic: files are processed by normalized
lexicographic path, then cards in each valid JSON document are ordered by
`(source_kind, external_key, card_revision_id, original_array_ordinal)`.
Duplicate copies of the same card therefore become idempotent no-ops. If
several revisions for one native source occur in one ingest batch, this order
deterministically selects the final lineage head; every immutable revision
remains auditable.

Lineage identity never uses a filesystem path or array position:

```text
EnrichedCardV2       → provenance.source_id
YouTubeSegmentCardV2 → segment_id, with parent/video metadata retained as evidence
```

The default lineage ID is a namespaced hash of native `source_kind` and
`external_key`. `card_revision_id` is
`cardrev:v1:sha256:<canonical-Wiki-payload-hash>`. The Wiki payload includes
content/language, summary, claim-bearing fields, structured entities/topics,
Wiki-relevant quality state, and source/evidence metadata. Generated
`search_text` and `graph_text` are excluded.

Collections declared semantically unordered are sorted by content fingerprint.
Quote text receives only Unicode NFC, CRLF-to-LF, and edge-trim normalization;
internal whitespace is evidence and is preserved. Segment-level YouTube time
ranges are source evidence, never occurrence-level exact locators.

The four overlapping inputs are:

```text
claim_inputs
structured_relation_inputs
card_projection_inputs
eligibility_inputs
```

`card_projection_inputs` contains source fields from which display/search text
will later be built, not generated `search_text`. `eligibility_inputs` contains
only schema/data facts (`quality_flags` and `schema_eligible`). Eligibility
policy belongs exclusively to the evaluator processor contract; changing only
that policy does not change a card revision or eligibility input generation.

### Processor contracts

| Table | Purpose |
|---|---|
| `processor_contract_versions` | Immutable method/prompt/model/schema/canonicalizer/policy/builder payload. |
| `processor_contract_activations` | Immutable activation history by stage generation. |
| `active_processor_contract_heads` | Current activation per stage kind. |

An activation history may be `A/1 → B/2 → A/3`; generation 3 references the
existing immutable A contract version. Calling activation with the already
active contract is a no-op.

Policies belong only to processor contracts. `DependencyKey` intentionally has
only `dependency_kind` and `dependency_scope_key`. `DependencyKind` is a closed
schema-v6 enum; extending it requires an explicit API and schema change.

### Universal data dependencies

| Table | Purpose |
|---|---|
| `dependency_versions` | Immutable data snapshot from ingest/manual/registry/stage. |
| `dependency_heads` | Universally addressable current snapshot and generation. |

Dependencies are data or state from another subsystem, never the method used to
process it. Examples are occurrence snapshots, approved identity-alias
snapshots, candidate sets, registry revisions, eligibility state, sidecars, and
effective grouping/link outputs.

Allowed schema-v6 kinds are:

```text
occurrence_snapshot
approved_identity_alias_snapshot
candidate_snapshot
registry_snapshot
surface_resolution
effective_claim_groups
effective_concept_links
eligibility_state
manual_sidecar
concept_display_snapshot
hierarchy_snapshot
card_relation_snapshot
card_projection_snapshot
claim_projection_snapshot
```

Policy, builder, model, prompt, schema, and canonicalizer names are rejected by
both the typed API and DDL checks.

`producer_kind` is one of `ingest`, `manual`, `registry`, or `stage`.
`produced_by_stage_version_id` is required only for stage outputs and is null
for all other producers.

### Stage DAG and attempts

| Table | Purpose |
|---|---|
| `lineage_stage_versions` | Immutable stage generation, aggregate binding hash, and exact contract activation. |
| `lineage_stage_heads` | Scheduler-owned current stage version. |
| `lineage_stage_input_bindings` | Exact lineage input generation/hash snapshot. |
| `stage_dependency_bindings` | Exact universal dependency generation/hash snapshot. |
| `stage_runs` | Preserved worker attempts and atomic apply result. |
| `outbox_events` | Committed-only downstream invalidation/events. |

`stage_inputs_hash` is computed from the complete sorted binding set. It is an
audit/identity aid, not a substitute for CAS. Final apply compares:

1. current stage head ID and generation;
2. active contract activation generation, version ID, and hash;
3. every input binding against `lineage_input_heads`;
4. every dependency binding against `dependency_heads`.

The scheduler/controller alone calls `schedule_stage` and advances a stage
head. Workers may start, fail, or apply attempts but cannot advance any
authoritative, input, dependency, contract, or stage head.

Every `stage_runs` row carries the `source_lineage_id`, `stage_kind`, and
`processor_contract_version_id` copied from its stage version. One composite
foreign key also binds its observed stage and contract-activation generations
to that exact immutable stage version. The `StageRun` API value returns these
copied stage-kind and contract fields. Further composite foreign keys bind the
run, artifact/applied card revisions, extraction run, occurrence, manifest, and
state event to the same lineage.

### Extraction and occurrence lifecycle

| Table/view | Purpose |
|---|---|
| `extraction_artifacts` | Reusable output keyed by claim inputs and processor contract. |
| `extraction_artifact_items` | Lineage-independent occurrence blueprints: field, payload, locator, evidence. |
| `extraction_runs` | Association between an artifact, stage attempt, lineage, and apply revisions. |
| `claim_occurrences` | Immutable lineage-bound occurrence payload/version. |
| `extraction_run_occurrences` | Exact run manifest. |
| `occurrence_state_events` | Immutable active/superseded/retired transition chain. |
| `occurrence_current_states` | Latest event from committed runs, ordered by `commit_seq`. |

Blueprint extraction is deterministic over `key_points`, `theses`, `quotes`,
and `events`; summary never becomes an occurrence. An artifact item stores
field kind, canonical exact payload and hash, occurrence fingerprint, stable
locator, and generic evidence metadata. The artifact key contains exactly the
`claim_inputs_hash` and deterministic hash of the active claim-extraction
processor contract (not its database-local UUID), so an identical claim input
can reuse one artifact across lineages and fresh databases.

Locator priority is an occurrence-level timestamp/source span/external locator
when actually present, otherwise field kind + content fingerprint + duplicate
ordinal. Array index is not identity. A source span is exact only with ordered,
finite, non-negative numeric `start`/`end` values. A mapping with an
`approximate` marker is rejected, and an `exact` marker must be `true`.
Timestamps are exact only when they are finite non-negative numbers or strict,
non-empty clock/ISO timestamp strings. Booleans, NaN/infinity, negative,
reversed, empty, malformed, and arbitrary object values safely fall back to a
content-fingerprint locator. Two identical occurrences receive distinct
deterministic duplicate ordinals and are never silently collapsed.
The occurrence fingerprint includes field kind, text/description, speaker,
event type/date, modality/type, and stance; it supports matching/audit but is
not an occurrence ID.

Occurrence identity uses lineage, field kind, stable locator, exact occurrence
payload hash, and occurrence schema version. A card-wide `claim_inputs_hash`
is provenance (`extracted_from_claim_inputs_hash`) and must not enter occurrence
identity.

An extraction artifact is valid only under a processor contract whose canonical
stage kind is `claim_extraction`. At artifact apply:

```text
artifact.processor_contract_version_id
  = extraction run contract
  = stage run/version contract

artifact.claim_inputs_hash
  = extraction run claim_inputs_hash
  = exact claim_inputs binding of the stage version
  = claim_inputs binding of artifact_source_card_revision_id
```

The artifact/run equalities use composite foreign keys. The two cross-table
input-binding equalities and canonical stage kind use a `BEFORE INSERT` guard.
The source and applied cards remain structurally bound to the run lineage. A
contract mismatch, missing binding, changed card binding, or non-extraction
stage cannot create an effective extraction apply. Each occurrence also binds
its `extracted_from_claim_inputs_hash` back to the exact extraction run hash.
The immutable occurrence's `card_revision_id` is its artifact source revision;
the extraction run separately records the newer card revision against which an
unchanged claim input was safely applied.

`previous_state_event_id` has a composite foreign key with
`occurrence_version_id` and `source_lineage_id`. Partial unique indexes permit
one root, one successor per event, and one event per extraction run/occurrence.

An occurrence event insert is rejected unless its extraction run points to an
already `committed` stage run with a non-null `commit_seq`. Started, failed,
stale, and no-op attempts therefore cannot poison current or future lifecycle
history.

`superseded` is allowed only when the predecessor is proved by the same unique
external locator (timestamp/source span or another stable external locator).
Content-fingerprint plus duplicate ordinal does not prove succession. In that
case the old occurrence becomes `retired` and a new occurrence becomes
`active`. Reactivation is an event transition to `active`, not a fourth status.

### Eligibility overlay

| Table/view | Purpose |
|---|---|
| `eligibility_evaluation_versions` | Immutable exact card/input/processor snapshot, reasons, and result. |
| `eligibility_heads` | Monotonic current evaluation and exact snapshot per lineage. |
| `lifecycle_active_occurrences` | Lifecycle-active occurrences before eligibility overlay; grouping/resolution may precompute from this view. |
| `effective_active_occurrences` | Lifecycle-active occurrences filtered by the current card/input/processor eligibility snapshot for review/projection. |

`extraction_unstable`, `no_substantive_content`,
`partial_segment_failure`, or failed schema eligibility make a lineage
ineligible and preserve audited reasons. Eligibility changes append evaluation
history and advance only the `eligibility_state` dependency. They do not
schedule claim extraction, recreate occurrences, or append occurrence state
events. Grouping and concept resolution bind the lifecycle occurrence snapshot,
not eligibility, so removing a flag reveals the same already prepared groups
and links without another resolver call.

Eligibility uses the canonical `eligibility_evaluation` stage and
`DEFAULT_ELIGIBILITY_CONTRACT`. Every evaluation stores the exact evaluated
card revision, eligibility input version/generation/hash, and active processor
contract activation/version/hash. DDL guards require those values to be current
both when inserting an evaluation and when moving its head.

The effective view repeats the current card, input, and processor joins. An old
eligibility result therefore becomes ineffective immediately after any of
those heads changes, even before a replacement evaluation is published. A
policy-only contract activation under unchanged card/data inputs appends a new
evaluation and advances the `eligibility_state` dependency generation; it does
not create a card revision, advance an input generation, or run claim
extraction.

### Approved concepts and review

| Table/view | Purpose |
|---|---|
| `concepts`, `concept_revisions`, `concept_heads` | Approved concept identity and immutable revisions. |
| `approved_concepts` | Hub-eligible concepts only. |
| `identity_aliases` | Approved identity-equivalent surfaces. |
| `surface_revisions`, `surface_heads` | Per-surface candidate revision, including surfaces with zero candidates. |
| `concept_proposals`, `concept_proposal_evidence` | Review candidates and their evidence. |
| `concept_review_decisions` | Immutable approve/reject/defer/reopen decisions. |
| `concept_proposal_current_decisions` | Latest decision by decision generation. |

`concepts` accepts only `approval_status = approved`; unapproved candidates
exist only as proposals. Identity alias kinds are canonical, technical,
abbreviation, translation, or spelling. `metonym` is forbidden.
An alias also has a composite foreign key to `(concept_revision_id, concept_id)`;
a revision owned by another concept cannot authorize the alias.

Registry scanning records every normalized surface revision, even with zero
candidate concepts. An ordinary entity/topic proposal requires both at least
two distinct substantive occurrence clusters and at least two independent
source families; byte-equivalent reposts remain one content cluster. A full
YouTube card and every segment whose `parent_source_id` points to it are one
source family, so parent/segment repetition cannot promote an incidental
mention. Each eligible segment still retains all claims and topics, and a
`primary` topic of a substantive YouTube segment may create a review proposal
from that one family. This exception does not apply to entities or
`secondary`/`mentioned` topics. A conflicting surface does not become a split
proposal until the second concept is actually represented by enough material.
Structured `entities.media_sources` values are provenance/search metadata, not
Wiki concepts: they never enter registry proposals or concept-linking candidate
snapshots. Publications such as Bloomberg or The Independent may still appear
inside attributed claims and source metadata, but cannot create their own hub.
The reviewer presents each candidate as a concrete hub/alias action and shows
claim, event, thesis, or quote examples from distinct source families; cluster
counts, hashes, and candidate keys are secondary technical details.
When the Luna profile is active, identity analysis receives those claim/event
contexts and may group grammatical forms (`Европа / Европе / Европы`),
abbreviations, translations, and spelling variants under a canonical dictionary
form. If only an inflected form exists, Luna may instead propose a reviewable
singleton canonicalization (`Украиной → Украина`); the observed form becomes a
technical alias only after approval. Cross-kind and conflicting entity-category
suggestions are rejected by deterministic validation after the model response.
Merge, canonicalization, and alias results remain review proposals: the only API
path to `concepts` or `identity_aliases` is an explicit approved review decision.
Successful analysis is cached by the exact candidate evidence snapshot, prompt
version, and model/profile version.

### Effective claims, links, and metonyms

| Table/view | Purpose |
|---|---|
| `claim_groups` | Reversible grouping target; occurrences are never physically merged. |
| `automatic_group_memberships` | Rebuildable grouping result. |
| `claim_group_overrides` | Authoritative assign/clear decision. |
| `effective_claim_group_memberships` | Active override, otherwise latest automatic result. |
| `occurrence_concept_automatic_links` | Rebuildable role-aware concept links with rule audit. |
| `occurrence_concept_link_overrides` | Authoritative include/exclude decision. |
| `effective_occurrence_concept_links` | Active override, otherwise latest automatic link. |
| `metonym_candidates` | Context-local resolver candidates. |
| `metonym_overrides` | Pinned/unresolved/rejected context-local decision. |

An override whose occurrence fingerprint no longer matches is appended as
`stale`; it stops applying but is never deleted. A metonym decision never
creates an identity alias or modifies the concept registry.

Relation roles are the enum:

```text
subject | actor | object | context | mentioned | unknown
```

`subject`, `actor`, and `object` aggregate as direct. `context` and `unknown`
aggregate as context; `mentioned` remains mentioned. Confidence is audit-only.
Explicit structured direct roles are deterministic and do not invoke Luna.
Luna is reserved for unclear roles and plausible claim-local metonyms, and an
unchanged relation stage reuses its recorded result.

### Card relations, hierarchy, sidecars, and projections

| Table/view | Purpose |
|---|---|
| `card_relations`, `card_relation_contributors` | Derived direct/context/mentioned relation and exact occurrence grounds. |
| `hierarchy_proposals` | Rebuildable primary-parent or related-edge proposal. |
| `approved_primary_hierarchy_edges` | Immutable approval/removal history; one effective primary parent. |
| `effective_primary_hierarchy_edges` | Current approved primary edge. |
| `approved_related_concept_edges` | Immutable canonical pair approval/removal history. |
| `effective_related_concept_edges` | Current approved related edges. |
| `llm_analysis_artifacts` | Immutable successful Luna identity/hierarchy results keyed by exact prompt inputs and model/profile version. |
| `manual_sidecars`, `manual_sidecar_heads` | Authoritative versioned Markdown maintained by the user. |
| `projection_artifacts`, `projection_heads` | Disposable card/claim/hub outputs and per-artifact input/output/FTS-document hashes. |
| `wiki_fts_documents`, `wiki_fts` | Rebuildable current projection documents and Unicode FTS5 index. |

Hierarchy tables reference `concepts`, which contains approved concepts only.
Automatic hierarchy work may create proposals but may not mutate approved
edges. Reviewer batch decisions are atomic: a cycle/limit failure rolls back
the whole batch. `wiki_fts_documents` is the rebuildable backing table for the external
content FTS5 table `wiki_fts`; insert/update/delete triggers keep it
synchronized. `fts_document_hash` separates card, claim, and hub projection
invalidation.

An unchanged identity or hierarchy analysis input reuses
`llm_analysis_artifacts` and never spends another model call. Changing the
candidate set, approved concepts/tree, prompt version, or model profile creates
a different cache key. Failed calls are not cached.

Projection identity is frozen by kind:

```text
card
  card_revision_id = projection_scope_key
  card_revision_id must exist in card_revisions
  concept_id = NULL
  claim_group_id = NULL

claim
  claim_group_id = projection_scope_key
  card_revision_id = NULL
  concept_id = NULL
  claim_group_id must exist in claim_groups

hub
  concept_id = projection_scope_key
  card_revision_id = NULL
  claim_group_id = NULL
  concept_id must exist in concept_heads
```

The `concept_heads` reference makes an effective approved concept mandatory for
every hub. A non-hub projection does not inherit that requirement.

`projection_inputs_hash`, `projection_output_hash`, and `fts_document_hash` are
always hashes of the individual artifact. For `projection_kind = 'claim'`,
these are specifically the claim projection's input, output, and FTS-document
hashes; they are not card- or hub-level hashes.

Every projection artifact has one composite foreign key binding
`produced_by_stage_version_id` and `processor_contract_version_id` to the same
stage version. A `BEFORE INSERT` guard enforces the canonical mapping:

```text
card  → card_projection
claim → claim_projection
hub   → hub_projection
```

For card projection, the guard additionally requires the card revision to
belong to the producer stage's lineage.

Every head has a non-null composite foreign key to the exact artifact identity,
generation, and all three hashes. Additional card-revision, concept, and
claim-group composite foreign keys bind the applicable non-null identity
despite SQLite's nullable-FK semantics. The kind-specific checks make the other
two identities null. Head generation may only increase.

`claim_projection_snapshot` is the explicit data dependency for a downstream
hub or future snippet stage that consumed a claim projection output hash. The
projection builder itself remains part of the processor contract, not that
dependency.

Hubs are built only for approved concepts with current eligible linked
material. Direct `subject`/`actor`/`object` claims and cards are the main view;
context and mentions remain present in collapsed sections. Hub Markdown stores
stable references to cards, not copied authoritative card state. Manual
sidecars live under a separate label-independent filename and are synchronized
to versioned `manual_sidecars` rows. Deleting generated hub files does not
delete approvals or sidecars.

## Transaction algorithms

### Authoritative card ingest

```text
BEGIN IMMEDIATE
  ensure immutable source lineage
  insert/reuse immutable card revision by lineage + content hash
  CAS-advance full card head if revision changed
  for every applicable input kind:
    canonicalize and hash
    if hash changed:
      append input generation
      CAS-advance that input head
    bind card revision to its first matching input version
COMMIT
```

### Dependency publish

```text
BEGIN IMMEDIATE
  read dependency head
  compare current_dependency_version_id with caller expected_version_id
  stale mismatch → rollback + StaleHeadError
  same hash → no-op
  changed hash → append generation and CAS-advance head
COMMIT
```

SQLite serializes competing writers at `BEGIN IMMEDIATE`. Each caller still
passes the exact version ID it observed before acquiring the write lock; a
second connection with a stale snapshot fails CAS after the first writer
advances the head.

### Stage scheduling

```text
BEGIN IMMEDIATE
  read active processor-contract activation
  read every declared input head and dependency head
  sort and hash the full binding snapshot
  unchanged snapshot + activation → no-op
  otherwise append immutable stage version and bindings
  CAS-advance lineage_stage_head
COMMIT
```

### Claim extraction atomic apply

```text
outside transaction:
  build/reuse lineage-free artifact blueprints
  prepare lineage-bound deterministic occurrence IDs

BEGIN IMMEDIATE
  load started attempt
  verify run stage kind and contract are structurally bound to its stage version
  existing committed idempotency key → mark attempt no_op
  compare stage head, contract activation, every input, every dependency
  mismatch → mark attempt stale; no commit_seq and no outbox
  verify current card still binds the exact staged claim_inputs hash
  read current committed occurrence state
  assign max(committed commit_seq) + 1 while write lock is held
  mark attempt committed
  insert extraction run, immutable occurrences, and complete manifest
  compute active/retired/superseded/reactivated transitions
  insert chained occurrence state events
  CAS-publish occurrence_snapshot dependency
  insert idempotent committed-only outbox event
COMMIT
```

The state diff is never accepted from a worker and is never calculated before
the final write lock. If any domain/event/dependency/outbox insert fails, the
transaction rolls back the run to `started` and leaves no extraction run,
occurrence, manifest, state event, occurrence dependency, or outbox event.
Artifact rows prepared before apply remain reusable.

### Eligibility evaluation

```text
prepare:
  activate/read the canonical eligibility_evaluation processor contract
  capture the current card revision
  capture exact current eligibility_inputs version/generation/hash
  evaluate flags/schema under that processor contract

BEGIN IMMEDIATE
  compare prepared card revision with source_lineage_heads
  compare exact input version/generation/hash and current card binding
  compare active contract activation/version/hash
  any mismatch → stale; no evaluation, head move, or dependency publish
  identical current snapshot → no-op
  append immutable evaluation generation
  CAS-advance eligibility head
  publish eligibility_state for the new evaluation generation
COMMIT
```

SQLite serializes writers, but the complete prepared snapshot remains the CAS
token. An evaluation prepared on card A cannot overwrite card B when A finishes
later. Direct SQL cannot bypass this rule because evaluation and head triggers
enforce the same snapshot.

The partial unique index permits only one committed attempt per idempotency
key. Failed, stale, and no-op attempts remain queryable. A failed attempt may be
retried with the same key. Outbox insertion is guarded by a trigger requiring a
matching committed run and `commit_seq`.

Outbox duplicate equality uses normalized canonical event fields plus canonical
`payload_json`, never Python object/dataclass equality. NFC/NFD-equivalent
events deduplicate; a truly different canonical payload under the same key is
an idempotency conflict.

`processed_at` implements a single-dispatcher delivery contract. Re-reading or
marking an event is idempotent. Multiple independent consumers require a future
per-consumer delivery table rather than overloading `processed_at`.

## Allowed and forbidden lifecycle

Allowed:

- append immutable versions/events;
- CAS a head to a newly appended, higher generation;
- reactivate an old contract payload through a new activation generation;
- reuse an extraction artifact whose key exactly covers its contents;
- apply extraction artifacts only through `claim_extraction`;
- emit card/claim/hub projections only from their mapped projection stage;
- append stale overrides and preserve the original manual decision;
- rebuild all derived rows and disposable projections.

Forbidden:

- UPDATE or DELETE of immutable source/version/domain rows;
- moving any head back to an old generation;
- choosing current state by timestamp;
- worker mutation of scheduler or authoritative heads;
- treating aggregate hashes as sufficient CAS evidence;
- placing dedup/relation/hub policies in dependencies;
- physical claim-occurrence merge or semantic successor guessing;
- converting a context metonym into an identity alias;
- hierarchy edges to unapproved concepts;
- outbox events from failed, stale, no-op, or started runs;
- global registry invalidation when only one surface/concept snapshot changed;
- artifact apply with a mismatched stage, contract, input hash, card binding, or
  projection identity.

## Public runtime API

Connection/schema:

- `connect_database`, `initialize_schema`;
- `CLAIM_EXTRACTION_STAGE_KIND`, `CARD_PROJECTION_STAGE_KIND`,
  `CLAIM_PROJECTION_STAGE_KIND`, `HUB_PROJECTION_STAGE_KIND`,
  `ELIGIBILITY_EVALUATION_STAGE_KIND`, `CLAIM_GROUPING_STAGE_KIND`,
  `CONCEPT_LINKING_STAGE_KIND`.

Canonicalization:

- `normalize_text`, `normalize_exact_quote`, `canonicalize`,
  `canonical_json`, `content_hash`, `content_fingerprint`.

State:

- `deterministic_source_lineage_id`, `ensure_source_lineage`,
  `record_card_revision`, `advance_input_head`,
  `get_input_head`;
- `activate_processor_contract`, `get_active_processor_contract`;
- `publish_dependency`, `get_dependency_head`;
- `schedule_stage`, `get_stage_version`;
- `start_stage_run`, `fail_stage_run`, `commit_stage_run`;
- `list_pending_outbox`, `mark_outbox_processed`.

Cards and ingest:

- `parse_card`, `adapt_card`, `load_card_file`, `discover_card_files`;
- `record_ingested_card`, `prepare_card_extraction`,
  `apply_prepared_extraction`, `ingest_card`, `ingest_path`;
- `DirectoryIngestStats` and per-path `IngestError`.

Extraction/lifecycle:

- `build_occurrence_blueprints`, `build_extraction_artifact`,
  `store_extraction_artifact`, `load_extraction_artifact`;
- `occurrence_version_id`, `prepare_lifecycle_apply`, `apply_lifecycle`;
- `DEFAULT_ELIGIBILITY_CONTRACT`, `prepare_eligibility_evaluation`,
  `apply_prepared_eligibility`, `evaluate_eligibility`,
  `get_current_eligibility`.

Registry/review:

- `scan_registry`, `list_proposals`, `approve_proposal`, `reject_proposal`,
  `defer_proposal`, `reopen_proposal`;
- `create_alias_proposal`, `create_identity_group_proposal`,
  `resolve_surface`, `list_concepts`, `update_concept_display`;
- `propose_identity_reviews_with_luna`.

Grouping/relations:

- `group_all_claims`, `group_lineage_claims`, `set_group_override`;
- `link_all_concepts`, `link_lineage_concepts`,
  `refresh_candidate_snapshot`, `set_concept_link_override`;
- `list_pending_ambiguities`, `resolve_ambiguity`.

Hierarchy/sidecars:

- `create_hierarchy_proposal`, `approve_hierarchy_proposal`,
  `reject_hierarchy_proposal`, `defer_hierarchy_proposal`,
  `reopen_hierarchy_proposal`, `list_concept_tree`;
- `propose_hierarchy_reviews_with_luna`;
- `save_manual_sidecar`, `get_manual_sidecar`, `sync_sidecars`.

Projection/retrieval/workflow:

- `build_card_projections`, `build_claim_projections`,
  `build_hub_projections`, `rebuild_all_projections`;
- `rebuild_wiki_fts`, `search_wiki`, `resolve_wiki_context`;
- `run_wiki_pipeline`, `run_configured_wiki_pipeline`,
  `refresh_wiki_after_review`, `get_wiki_review_counts`, `wiki_status`.

All authoritative decisions are append-only. Derived output APIs own short
transactions, recheck their complete stage bindings before apply, and can be
rerun idempotently. The shared Streamlit reviewer is the user-facing mutation
surface; normal `enrich`/`run` invokes the configured Wiki workflow and opens
that reviewer whenever any content or Wiki decision is pending.
