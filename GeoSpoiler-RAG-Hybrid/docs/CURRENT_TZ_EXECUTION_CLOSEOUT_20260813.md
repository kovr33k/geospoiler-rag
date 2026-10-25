# CURRENT TZ execution closeout — 2026-08-13

Status: **BLOCKED_EXTERNAL_LUNA_CREDITS**

The implementation and native-media backfill are complete. Publication to
LightRAG is intentionally not performed because 67 generic cards still require
semantic re-enrichment after the Codex Luna workspace returned `out of credits`.
The previously published corpus was restored and remains complete.

## Completed

- Native media: 95/95 visual extractions succeeded through Luna.
- Native video/audio: 21/23 transcribed through OpenRouter/Gemini; 2/23 were
  valid `no_speech` results; technical failures are zero.
- Persisted media paths are portable; absolute legacy paths, degenerate
  transcripts, update journals, and temporary files are zero.
- A degenerate STT response was detected, rejected, retried, and replaced by a
  valid result. STT cache identity now requires input SHA and active model.
- Live media actions require explicit `--execute-live`; dry-run is the default.
- Visual and STT terminal statuses, attempts, latency, model identity, and
  artifacts are persisted atomically.
- Backup/restore is fail-closed and covers normalized data, transcripts,
  visuals, enriched cards, indexes, progress, and LightRAG.
- Full pre-run backup validation: 443/443 files, SHA/path/SQLite checks passed.
- LightRAG provenance reconciliation: 129/129 matched, zero missing,
  ambiguous, or nonterminal records.
- Provider failure now retains the previous published card and does not mark
  progress current. Retrieval refresh cannot truncate an existing corpus after
  an external outage.
- Non-media cards with identical fingerprint/schema/prompt/model are migrated
  to `media-contract-v2` without an unnecessary LLM call.

## Current safe production state

- Enriched cards: 131/131 valid JSON.
- Card FTS: 131.
- Registry: 133 sources, 129 normalized documents, 131 enriched cards,
  32 references.
- LightRAG: 129/129 processed; graph was not partially updated.
- Generic progress: 127 entries; 60 are `media-contract-v2`.
- Pending semantic re-enrichment: 67 generic cards.
- Four YouTube episode cards use their separate YouTube contract and are not
  counted as generic media-contract backlog.

## Verification

- Ruff: passed.
- Pytest: 674 passed, 1 skipped, 42 subtests passed.
- Repository security scan: 4,056 files scanned, zero findings.
- Full backup revalidation after execution: 443/443 passed.
- Active enrichment/media writers at closeout: zero.

## Required continuation

1. Replenish Codex Luna workspace credits.
2. Run `python main.py enrich --llm-profile luna` again. The progress/fingerprint
   contract will skip completed cards and retry the 67 pending generic cards.
3. Require 127/127 generic progress/card `media-contract-v2` and zero failures.
4. Rebuild FTS and registry, generate a strict accepted manifest for the changed
   cards, then update LightRAG through the accepted-manifest loader only.
5. Repeat graph provenance, test, lint, backup, and security gates before READY.

Machine evidence:

- `artifacts/current_tz_execution_status_20260813.json`
- `artifacts/graph_provenance_reconciliation_20260813_final.json`
- `artifacts/security_release_scan_20260813_final.json`
- `artifacts/luna_full_reenrich/current_tz_media_20260813_v2/backup_restore_validation_final.json`
