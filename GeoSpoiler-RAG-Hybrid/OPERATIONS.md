# Operations

This document is the operational runbook for the local GeoSpoiler RAG project.
It focuses on safe day-to-day commands, rebuilds, generated artifacts, and
recovery checks.

## Python And Encoding

Preferred interpreter:

```powershell
.\.venv\Scripts\python.exe
```

For PowerShell sessions that print Cyrillic paths or answers:

```powershell
$env:PYTHONIOENCODING="utf-8"
$env:PYTHONUTF8="1"
```

On Windows, `.\run_pipeline.cmd ...` is useful for common pipeline commands
because it resolves a real Python interpreter and sets UTF-8 environment
variables.

Use direct Python for newer subcommands that are not exposed through
`run_pipeline.ps1`:

```powershell
python main.py fts rebuild
python main.py registry rebuild
python main.py transcribe backfill --dry-run
python main.py experiments index
```

## Runtime Configuration

Keep secrets in `.env`; use `.env.example` only as a template. These are the
operationally important flags to check before runs:

| Variable | Purpose | Current trusted note |
|---|---|---|
| `LLM_BASE_URL`, `LLM_MODEL` | Default OpenAI-compatible chat endpoint/model | API-profile settings; the active text profile is Luna (Codex CLI). |
| `RAG_BUILD_*` | Build-time chat endpoint/model override | Empty values fall back to `LLM_*`. |
| `QUERY_*` | Query-time chat endpoint/model override | Use for live query/golden runs. |
| `FALLBACK_SYNTH_*` | Hybrid synthesis endpoint/model override | Keep the configured profile; do not switch models during structure validation. |
| `ENRICHMENT_*` | Enriched-card endpoint/model/schema/concurrency override | Use the same provider family unless testing; `ENRICHMENT_CONCURRENCY` defaults to `LLM_MAX_ASYNC`. |
| `TRANSLATION_*` | Translation endpoint/model override | Empty values fall back to `LLM_*`. |
| `LLM_MAX_ASYNC` | Chat request concurrency | Higher values are useful only with a paid/stable provider. |
| `LLM_DELAY_SECONDS`, `RAG_BUILD_DELAY_SECONDS` | Artificial pacing between LLM calls | Use `0.0` only when the provider quota can handle it. |
| `QUERY_MAX_TOKENS`, `FALLBACK_SYNTH_MAX_TOKENS` | Query/synthesis output budgets | Tune for long source-grounded answers. |
| `LLM_REASONING_EFFORT` | Generic OpenAI-style reasoning effort | Applies to the API profile; Luna uses CODEX_LUNA_REASONING_EFFORT. |
| `HYBRID_QUERY_CARDS_ENABLED` | Attach enriched-card context to query | Keep `true` for trusted query runs. |
| `WIKI_ENABLED` | Master switch for all Wiki ingest, review, projections, CLI, and retrieval | Keep `false`; the dormant implementation and existing state remain available for later. |
| `HYBRID_QUERY_WIKI_ENABLED`, `HYBRID_QUERY_WIKI_TOP_K` | Attach approved Wiki hubs when the master switch is on | Ignored while `WIKI_ENABLED=false`. |
| `HYBRID_SYNTH_ENABLED` | Synthesize final answer from graph/cards | Keep the existing configured value. |
| `RERANKER_ENABLED` | Enable reranker hook | Keep `false`; the working profile preserves the previously accepted setting. |
| `TRANSCRIPTION_ENABLED` | Enable native media transcription | Keep `false` unless testing real downloaded media. |

Instagram Reel extraction uses the `yt-dlp` module from the active Python
interpreter, not a globally installed executable. Verify the runtime before a
media backfill:

```powershell
.\.venv\Scripts\python.exe -m yt_dlp --version
```

The project requires `yt-dlp>=2026.7.4`; older global versions can return
`Instagram sent an empty media response` even when the Reel is publicly
playable in a browser.

## Normal Daily Flow

Check current state:

```powershell
python main.py status
```

See which posts did not become an Enriched v2 card and why. The command only
reads; each post gets one label: `review` (ожидает ревью), `retry` (ожидает
повтора), `failed` (ошибка обработки) or `policy` (пропуск по политике). Open
normalization and channel fetch failures come from
`data/state/ingestion_outcomes.json`; `run` ends with a one-line problem count.

```powershell
python main.py ingest-report
python main.py ingest-report --channel "Channel title" --category failed
python main.py ingest-report --json
```

Fetch and normalize new Telegram messages without loading LightRAG:

```powershell
python main.py normalize
```

Enrich normalized posts into evidence cards:

```powershell
python main.py enrich
```

Load enriched v2 graph_text into the active LightRAG storage:

```powershell
python main.py load
```

Fetch -> normalize -> enrich -> refresh local indexes (LightRAG load stays explicit):

```powershell
python main.py run
```

Use `run` only when you are comfortable doing all steps in one pass.

## Generated Artifacts

Important local paths:

```text
data/normalized/          source-of-truth normalized text and .meta.json files
data/enriched/            structured evidence cards
data/review_queue/        manual review queue for AI chats, external links, long Reels, and low-info posts
data/transcripts/         native media transcript artifacts
data/indexes/card_fts.sqlite local FTS index for enriched cards
data/indexes/source_registry.sqlite source registry
data/wiki/state.sqlite    versioned Wiki state and review history
data/wiki/projections/    disposable generated Wiki hubs
data/wiki/sidecars/       authoritative manual hub notes
data/indexes/lightrag/    active LightRAG storage
rag_storage_backups/      rebuild backups
logs/                     pipeline and LightRAG logs
data/state/               Telegram/progress state
data/cache/media/         downloaded images and native media
```

Do not delete generated data casually. If a rebuild or backfill needs to touch
these paths, prefer commands that make the operation explicit.

## FTS Index

Rebuild the local card FTS index:

```powershell
python main.py fts rebuild
```

Search it:

```powershell
python main.py fts search "Trump Orban" --top-k 5
```

Compare with legacy shadow search:

```powershell
python main.py fts search "Trump Orban" --top-k 5 --compare-shadow
```

FTS rebuild is local and does not call an LLM.

## Source Registry

Rebuild source registry from normalized metadata and enriched cards:

```powershell
python main.py registry rebuild
```

Resolve a source id:

```powershell
python main.py registry resolve telegram:3328128766:148
```

Run registry rebuild after major normalized/enriched changes if query source
resolution looks stale.

## Approved Wiki

Wiki is dormant while `WIKI_ENABLED=false`: `enrich`/`run` does not ingest or
refresh it, queries do not search it, the reviewer hides its tab, and Wiki CLI
commands are blocked. Existing SQLite state and generated files are preserved.

To reactivate it later, set `WIKI_ENABLED=true`. The normal `enrich`/`run` flow
will then refresh Wiki automatically. It can also be run directly:

```powershell
python main.py wiki run
python main.py wiki run --llm-profile luna
python main.py wiki run --no-luna
```

The Luna form may propose identity equivalence, claim-local resolutions, and a
sparse hierarchy. It still cannot approve a concept, alias, ambiguity, or
edge. `--no-luna` keeps registry thresholds, exact grouping, deterministic
roles, hubs, and FTS active while making no Wiki-side Luna calls.

Review all content and Wiki queues in one browser:

```powershell
python main.py review --web
# equivalent Wiki-specific entry point
python main.py wiki review
```

Inspect and search:

```powershell
python main.py wiki status
python main.py wiki search "КНДР ракетная программа"
```

Recompute derived groups, links, hubs, and FTS without rereading card files:

```powershell
python main.py wiki rebuild
```

Recovery rules:

- deleting `data/wiki/projections/` is safe; `wiki rebuild` recreates it;
- do not delete `data/wiki/sidecars/` or `data/wiki/state.sqlite` without an
  explicit backup, because they contain manual/approved state;
- an old SQLite schema is rejected rather than silently migrated;
- for a planned clean reload, archive/remove the exact Wiki DB explicitly and
  run `wiki run`; Enriched cards remain the source material;
- generated hub files marked `generated_by: geospoiler-wiki-v2` may be
  replaced or removed by the builder; unrelated files in that directory are
  not deleted.

## Experiment Registry

Rebuild the local experiment registry after meaningful golden/probe/smoke
runs:

```powershell
python main.py experiments index
```

Outputs:

```text
artifacts/experiment_registry.json
artifacts/experiment_registry.md
```

This command reads existing `*_scores.json` artifacts only. It does not call a
live model and does not modify RAG storage.

## Native Media Transcription

Transcription is controlled by:

```text
TRANSCRIPTION_ENABLED
TRANSCRIPTION_API_KEY
TRANSCRIPTION_BASE_URL
TRANSCRIPTION_MODEL
TRANSCRIPTION_LANGUAGE
TRANSCRIPTION_TIMEOUT_SECONDS
```

New native media transcripts are attached during normalization when
`TRANSCRIPTION_ENABLED=true` and downloaded media is available.

Telegram, Instagram and YouTube audio use OpenRouter `/audio/transcriptions`
with the single `TRANSCRIPTION_MODEL` (`openai/whisper-large-v3` by default).
Video files are converted to mono mp3 before upload. There is no second model
or chat-audio fallback. Failed requests retain their status, response detail and
attempt timing instead of being treated as successfully processed.

For old downloaded media, use controlled backfill:

```powershell
python main.py transcribe backfill --limit 3 --dry-run
python main.py transcribe backfill --limit 3 --media-type voice
```

Backfill updates existing normalized `.txt` files only when a transcript is
available and also updates the `.meta.json` sidecar.

After backfilling transcripts, rerun:

```powershell
python main.py enrich
python main.py fts rebuild
python main.py registry rebuild
```

Do not run a large transcription backfill without first doing a dry run.

## AI Chat Review Queue

Show pending review items:

```powershell
python main.py review
```

Review files live in:

```text
data/review_queue/
```

You can inspect the queue from the CLI or launch the Streamlit reviewer:

```powershell
python main.py review
python main.py review --web
```

To process one item, edit its JSON:

```json
{
  "status": "processed",
  "extracted_text": "..."
}
```

Then run:

```powershell
python main.py load
```

The load step includes all review queue items with `status=processed` and non-empty `extracted_text`.
Review JSON files are treated as their own stable source paths.

Do not edit `data/normalized/*.txt` to paste reviewed external-link text unless you intentionally want to replace the original normalized post. Prefer `python main.py review --web`, which stores reviewed text in the review JSON.

## Query Modes

Common direct query:

```powershell
python main.py query "question" hybrid
```

Use `source` profile for provenance-style questions:

```powershell
python main.py query "Where is this claim from?" hybrid source
```

Search without live LLM:

```powershell
python main.py search "Narva Estonia" --mode cards
python main.py fts search "Narva Estonia" --top-k 5
```

Keep `RERANKER_ENABLED=false` until reranker comparisons have been explicitly
run and accepted.

## LightRAG Rebuild

Normal operations should not rebuild LightRAG.

Use rebuild only when the active graph must be recreated from current source
files:

```powershell
python main.py rebuild
```

This moves the existing `data/indexes/lightrag/` into `rag_storage_backups/`
and loads non-empty `graph_text` from accepted `data/enriched/*.enriched.json`
cards. It does not reload raw normalized text or review-queue items.

## Rebuild And Recovery Runbook

Use this sequence when you intentionally need a clean graph from current
enriched cards:

```powershell
python main.py fts rebuild
python main.py registry rebuild
python main.py rebuild
python main.py fts rebuild
python main.py registry rebuild
```

Then validate behavior:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python tests/test_golden_set.py
python main.py experiments index
```

If rebuild quality regresses:

1. Stop further loads.
2. Keep the failed `data/indexes/lightrag/` by moving or renaming it with a timestamp.
3. Restore the latest known-good directory from `rag_storage_backups/`.
4. Run `python main.py status` and a local FTS search before another live probe.
5. Record the failed attempt and artifact paths in `DEVELOPMENT_RETURN_LOG.md`.

Do not use enriched-card graph rebuild as a recovery path. It previously proved
slow/unstable on the full corpus and was removed from the main CLI.

## Evaluation After Operational Changes

After code changes:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

After retrieval/source changes:

```powershell
python main.py fts search "Trump Orban" --top-k 5
python main.py registry resolve telegram:3328128766:148
```

For live model quality checks, follow `EVAL.md`.

## Common Recovery Notes

If PowerShell corrupts Cyrillic inline strings, use UTF-8 files, Unicode
escapes, or set:

```powershell
$env:PYTHONIOENCODING="utf-8"
$env:PYTHONUTF8="1"
```

If query answers look stale after changing models, inspect LightRAG response
cache before trusting the run. Focused probes can back up and clear cache with:

```powershell
$env:LLM_PROBE_CLEAR_CACHE="true"
python llm_verification_probe.py
```

If source grounding is wrong, check retrieval/source layers before blaming the
LLM endpoint:

```powershell
python main.py fts search "query terms" --top-k 10 --compare-shadow
python main.py registry resolve SOURCE_ID
```

If SQLite files are locked on Windows, make sure no long-running process still
has the DB open, then rerun the command. Unit tests should close SQLite
connections explicitly.

## Logs

Pipeline logs:

```text
logs/pipeline_YYYYMMDD.log
```

LightRAG/UI logs:

```text
logs/lightrag.log
logs/lightrag_ui.log
logs/lightrag_ui.err.log
```

Record unresolved operational issues in `DEVELOPMENT_RETURN_LOG.md`. Record
live model/provider checks in `LLM_VERIFICATION_QUEUE.md`.

## Structure migration and recovery

Install with `python -m pip install -e .`. Both `python main.py` and `python -m geospoiler` use the same dispatcher. An installed wheel needs an absolute `GEOSPOILER_PROJECT_ROOT` pointing to the directory containing `.env` and data. Source/editable installs locate their own checkout, independent of CWD.

Active layout: `data/normalized`, `data/enriched`, `data/enriched_segments`, `data/indexes`, `data/state`, `data/cache`. `data/.layout-v2.json` preserves the original identity root; do not delete it or rewrite source paths in saved cards. The resolver maps legacy paths to current files and keeps document IDs unchanged.

Migration commands (application stopped):

```powershell
python -m geospoiler.operations.migrate_layout plan --root D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid
python -m geospoiler.operations.migrate_layout stage --root D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid
python -m geospoiler.operations.migrate_layout activate --root D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid
python -m geospoiler.operations.migrate_layout rollback --root D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid
```

Rollback refuses to overwrite new/changed data. If the application has written after activation, preserve those writes and reconcile them before restoring. `backups/structure-layout` includes the original `.env` and is private local data. The external pre-migration copy is documented in the execution report.

Wiki must report both effective flags false in the working runtime. Existing Wiki projection files do not mean Wiki is enabled. Environment variables take precedence over `.env`; check the effective settings, not just the file.
