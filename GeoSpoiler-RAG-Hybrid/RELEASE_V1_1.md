# GeoSpoiler RAG v1.1.0 Release

Release date: 2026-06-02

## Status

v1.1.0 is the hardened release after v1.0.0. It keeps the same product
direction and focuses on maintainability, source-grounding quality, retrieval
robustness, and removing unsupported experimental rebuild paths from the main
workflow.

## Highlights

- Split loader responsibilities into clearer client, postprocess, and reference
  helper boundaries.
- Added a dedicated source-selection golden runner for provenance/source
  grounding.
- Improved card-context retrieval ranking so direct evidence is preferred over
  broad adjacent sources.
- Fixed the historical Q22 Narva visual source-ranking failure.
- Documented remaining retrieval guardrails and their removal policy.
- Retired enriched-card graph rebuild from the supported main CLI path.
- Kept enriched cards as a retrieval/context layer through FTS, source registry,
  wiki memory, and hybrid query context.

## Trusted Runtime

- Query/eval model: `deepseek-v4-flash`
- Query mode: `hybrid`
- `RERANKER_ENABLED=false`
- `HYBRID_SYNTH_ENABLED=true`
- `HYBRID_QUERY_CARDS_ENABLED=true`
- `WIKI_ENABLED=true`

## Final Verification

- `python -m unittest` -> `156` tests OK
- Full golden -> `23/23`, average `100.0`
- Source-selection golden -> `10/10`, average `100.0`
- `python main.py status` -> `220` normalized files, `0` pending reviews
- `python main.py wiki health` -> `22` pages checked, `0` issues
- `python main.py experiments index` -> `27` active records

## Final Artifacts

- `artifacts/v1_1_release_full_golden_results.md`
- `artifacts/v1_1_release_full_golden_scores.json`
- `artifacts/v1_1_release_source_selection_results.md`
- `artifacts/v1_1_release_source_selection_scores.json`
- `artifacts/experiment_registry.md`

## Accepted v1.1 Debt

- Telegram re-auth is currently an external operational issue and is not needed
  for the release checks. Local `state/` remains git-ignored.
- Live transcription remains pending until real downloaded audio/video/voice
  candidates exist.
- At v1.1 release time, the ultra-left/right source hint remained a documented
  safety net because removing it still put canonical source `3299898370/11`
  below rank 1.
- B2 enriched-card warnings remain non-blocking data cleanup debt.
- Ragas/Phoenix-style observability and deeper architecture cleanup are v2 work.

## Post-Release Resolved Debt

- 2026-06-09: the ultra-left/right source hint was removed after local retrieval
  was fixed to rank canonical source `3299898370/11` first without hardcoded
  source injection. See `RETRIEVAL_GUARDRAILS.md` and
  `artifacts/source_hint_removal_full_source_selection_results.md`.
  Post-removal checks: `python -m unittest` -> `193` tests OK; source-selection
  golden -> `10/10`; full golden -> `23/23`, average `100.0`.

## Supported Rebuild Path

The supported graph rebuild path is:

```powershell
python main.py rebuild
```

This rebuilds from `output/normalized/`, the source-of-truth text layer.
`--from-enriched` is not a supported release path.
