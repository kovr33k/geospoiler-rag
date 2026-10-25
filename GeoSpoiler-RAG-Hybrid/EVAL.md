# Current comparison workflow

Before the structure migration: **1152 passed, 4 skipped, 44 subtests passed** on 2026-09-09 after repairing the Windows test socketpair guard and preparing tokenizer cache. These are historical baseline results, not the final post-migration verdict. [Execution and final status](docs/STRUCTURE_MIGRATION_EXECUTION_20260909.md).

Final post-migration result on 2026-09-10: **1178 passed, 4 skipped, 44
subtests passed**. The four skips require a Windows directory-symlink privilege
which was unavailable to the test token; their exact reasons are retained in
the external test log linked from the execution evidence directory.

After the 2026-09-10/11 live follow-up normalized successful smoke reports from
`fallback: null` to `fallback: false`, the full offline result is **1179 passed,
4 skipped, 44 subtests passed**. The additional test verifies the complete
strict smoke contract for a runtime result whose optional fallback value is
`None`.

Prepare tokenizer data once before blocking the network:

```powershell
$env:TIKTOKEN_CACHE_DIR = Join-Path $env:LOCALAPPDATA "GeoSpoiler\tiktoken"
.\.venv\Scripts\python.exe -c "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')"
```

Then run offline tests through the maintained isolated runner. It redirects
every writable runtime path and pytest basetemp outside the checkout:

```powershell
.\scripts\run_offline_tests.ps1 -RunRoot D:\GeoSpoiler-test-runs\unique-run
```

`run_tests.ps1` asks live questions; it is not the unit suite. Full corpus integrity checks do not require LLM regeneration. Live answer checks run separately, after applicable review gates, with saved answers, source IDs, citations, URLs and a bounded question set. Compare deterministic search results/IDs exactly; compare LLM responses by grounding and usefulness, not byte equality.

# Evaluation

This document describes the current GeoSpoiler RAG evaluation workflow.
It separates deterministic unit checks from live LLM quality checks.

## Test Layers

Use these layers in order:

1. Unit tests: no network, no real Telegram, no live LLM.
2. Local health/index checks: deterministic filesystem checks.
3. Baseline probe: small live query sanity check.
4. Golden set: full live query comparison.
5. Source-selection golden: focused live source retrieval/provenance checks.
6. Focused LLM probe: targeted live checks for known source-grounding failures.

Do not treat live LLM probes as unit tests. They measure model, retrieval, and
source-grounding behavior under the current runtime configuration.

## Historical DeepSeek Results

The results below describe the former `deepseek-v4-flash` profile. The active
text profile is now configured independently (the checked-in example uses
`LLM_PROFILE=luna`); do not treat these historical scores as a current live result.

Recommended trusted flags:

```text
RERANKER_ENABLED=false
HYBRID_SYNTH_ENABLED=true
```

Historical results for the profile described in this section:

- Focused probe: `6/6`, average score `100.0`.
- Full golden set before rebuild: `23/23`, average score `100.0`.
- Full golden set after clean rebuild: `23/23`, average score `100.0`.
- Full golden set for v1.1.0 release: `23/23`, average score `100.0`.
- Source-selection golden: `10/10`, average score `100.0`; Q22 Narva visuals now ranks the direct source first.
- Reranker experiment: `20/23`, average score `96.7`; keep reranker disabled.

Primary artifacts:

```text
artifacts/llm_probe_deepseek_v4_flash_absence_fix_clearcache_scores.json
artifacts/deepseek_v4_flash_clearcache_golden_set_scores.json
artifacts/deepseek_v4_flash_rebuilt_clearcache_golden_set_scores.json
artifacts/v1_1_release_full_golden_scores.json
artifacts/v1_1_release_source_selection_scores.json
artifacts/deepseek_v4_flash_reranker_clearcache_golden_set_scores.json
artifacts/experiment_registry.md
```

## Unit Tests

Run all no-network tests with:

```powershell
python -m pytest tests
```

Use the project Python when running from automation:

```powershell
.\.venv\Scripts\python.exe -m unittest
```

Relevant focused suites:

```powershell
python -m pytest tests/test_golden_set_unit.py tests/test_golden_compare.py tests/test_baseline_probe.py
python -m pytest tests/test_media_capture.py tests/test_transcription_backfill.py
```

## Local Health Checks

FTS sanity:

```powershell
python main.py fts rebuild
python main.py fts search "Trump Orban" --top-k 5
```

Source registry sanity:

```powershell
python main.py registry rebuild
python main.py registry resolve telegram:3328128766:148
```

These checks are local and should not call a live model.

## Baseline Probe

The baseline probe records the active query model/config and can run a small
manual query set.

```powershell
python main.py baseline probe 3
```

Artifacts:

```text
artifacts/baseline_model_probe_metadata.json
artifacts/baseline_model_probe_results.md
```

For isolated baseline work, prefer:

```text
RERANKER_ENABLED=false
HYBRID_SYNTH_ENABLED=false
HYBRID_QUERY_CARDS_ENABLED=true
```

Use a cache buster only when stale LightRAG cache would invalidate the run:

```powershell
$env:BASELINE_PROBE_CACHE_BUSTER="run-20260528"
python main.py baseline probe 3
```

## Golden Set

The full golden set lives in `test_golden_set.py`; the human-readable question
guide lives in `GOLDEN_SET.md`.

Run a small smoke subset:

```powershell
$env:GOLDEN_CASE_LIMIT="3"
python tests/test_golden_set.py
```

Run the full golden set:

```powershell
python tests/test_golden_set.py
```

Useful output overrides:

```powershell
$env:GOLDEN_RESULTS_FILE="artifacts/my_run_results.md"
$env:GOLDEN_SCORES_FILE="artifacts/my_run_scores.json"
python tests/test_golden_set.py
```

Retry/delay controls:

```text
GOLDEN_QUERY_DELAY_SECONDS
GOLDEN_QUERY_RETRIES
GOLDEN_RETRY_BACKOFF_SECONDS
```

Artifacts:

```text
artifacts/golden_set_results.md
artifacts/golden_set_scores.json
```

Golden scoring checks:

- required answer terms;
- forbidden hallucination/technical-leakage terms;
- source-required questions;
- expected source selection for known cases;
- per-case pass/fail and average score.

## Source-Selection Golden

The source-selection golden lives in `source_selection_golden.py`; usage details
live in `SOURCE_SELECTION_GOLDEN.md`.

It is a dedicated live-eval layer for retrieval/source grounding. It checks that
canonical evidence appears in the user-visible sources within the expected rank,
and it can fail when a broad near-miss document outranks direct evidence even if
the answer text still looks acceptable.

Run the full source-selection golden:

```powershell
python source_selection_golden.py
```

Run selected cases:

```powershell
$env:SOURCE_GOLDEN_CASE_IDS="q9_cuba_protests_source,q22_narva_visuals_top_source"
python source_selection_golden.py
```

Current source-selection state:

- Phase 2 baseline: `9/10`, average score `90.0`;
- Phase 3 final: `10/10`, average score `100.0`;
- selected Q9 Cuba protests check: `1/1`, average score `100.0`;
- fixed Phase 3 case: `q22_narva_visuals_top_source`, where direct Narva source
  `3889026624/2` now appears at rank 1.

Artifacts:

```text
artifacts/v1_1_phase2_source_selected_scores.json
artifacts/v1_1_phase2_source_selected_results.md
artifacts/v1_1_phase2_source_selection_scores.json
artifacts/v1_1_phase2_source_selection_results.md
artifacts/v1_1_phase3_source_selection_scores.json
artifacts/v1_1_phase3_source_selection_results.md
```

## Golden Comparison

Compare two golden score JSON files:

```powershell
python golden_compare.py artifacts/baseline_scores.json artifacts/candidate_scores.json --output artifacts/compare.md
```

Use this after changes to:

- retrieval ranking;
- synthesis prompts;
- reranker configuration;
- model/provider settings.

Regressions to inspect first:

- pass to fail;
- negative score delta;
- `source_ok` or `source_any_ok` getting worse;
- focus questions about sources, fake/deepfake separation, or provenance.

## Focused Live LLM Probe

`llm_verification_probe.py` is a small live probe for known weak points:

- F1 Trump/Orban source grounding;
- Q7 Cuba talks;
- Q9 Cuba protests;
- Q22 Narva visuals;
- North Korea source-control query;
- AfD funding absence-control query.

Run:

```powershell
python llm_verification_probe.py
```

Optional cache clear:

```powershell
$env:LLM_PROBE_CLEAR_CACHE="true"
python llm_verification_probe.py
```

Output overrides:

```powershell
$env:LLM_PROBE_RESULTS_FILE="artifacts/probe_results.md"
$env:LLM_PROBE_SCORES_FILE="artifacts/probe_scores.json"
python llm_verification_probe.py
```

## Reranker Evaluation

Keep reranker evaluation separate from synthesis changes.

Recommended sequence:

1. Run golden with `RERANKER_ENABLED=false`.
2. Run the same golden with `RERANKER_ENABLED=true`.
3. Keep `HYBRID_SYNTH_ENABLED` explicit in both runs.
4. Compare with `golden_compare.py`.
5. Inspect latency, source grounding, and unsupported confident claims.

Do not accept the reranker only because average score improves; source
grounding must not regress.

## Experiment Registry

Use the local registry after golden/probe/smoke runs to get one compact view of
all score artifacts:

```powershell
python main.py experiments index
```

Outputs:

```text
artifacts/experiment_registry.json
artifacts/experiment_registry.md
```

The registry is read-only over existing `*_scores.json` files. It does not run
new LLM calls and should be regenerated after meaningful eval runs.

## Recording Results

Use:

- `DEVELOPMENT_RETURN_LOG.md` for development breadcrumbs, blocked checks, and
  local implementation notes.
- `LLM_VERIFICATION_QUEUE.md` for live model/provider checks, model candidates,
  endpoint failures, and source-grounding issues that require real LLM behavior.

When a live run matters, record:

- date;
- command;
- model and endpoint;
- key env flags;
- pass count and average score;
- artifact paths;
- failed cases and likely cause.

## Safety Rules

- Do not run LightRAG rebuild as part of eval unless the change explicitly
  requires it.
- Do not mix model change, retrieval change, synthesis change, and reranker
  change in one comparison.
- Clear LightRAG LLM cache only when stale cross-model answers would invalidate
  the result, and keep the backup path.
- Treat source-grounding failures as retrieval/evidence issues first, not as
  proof that the LLM endpoint is unusable.
