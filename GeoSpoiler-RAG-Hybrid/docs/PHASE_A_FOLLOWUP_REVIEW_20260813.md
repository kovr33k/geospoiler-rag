# Phase A follow-up review — 2026-08-13

## Closed in code

- Re-enrichment preflight no longer hard-codes the historical `131` normalized-document count. It validates live accounting (`normalized_documents == generic_jobs + youtube_only_documents`), one unique source identity per normalized document, and exactly four YouTube sources/video IDs.
- The current post-deletion corpus (`129` normalized documents in the operator's checkout) is therefore accepted when its accounting is internally consistent.
- Media/card synchronization now has a contract version, portable media paths, atomic transcription updates, explicit backfill selectors, and route/attempt/latency diagnostics.

## Residual operator gates

- Do not run live re-enrichment or media backfill as part of this code change. First create the immutable backup run directory and inspect its preflight report.
- Existing historical sidecars may still contain absolute paths; the runtime resolver remains backward-compatible, while newly written sidecars use portable paths. A separately authorized migration is required to rewrite old production sidecars.
- The preflight still requires the active Luna model, `CODEX_FALLBACK_TO_API=false`, `WIKI_ENABLED=false`, and `RERANKER_ENABLED=false`.

## Evidence

- `tests.test_luna_reenrich_control`: 3 passed.
- The Phase A media/backfill regression suites remain green; no production data was modified and no live backfill was executed.
