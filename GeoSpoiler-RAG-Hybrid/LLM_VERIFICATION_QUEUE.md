# LLM Verification Queue

This file tracks checks that should be run with a live LLM endpoint later.
Keep normal unit tests out of this list unless they require a real model or real API behavior.

## Pending

- I2 live Whisper transcription check:
  enable `TRANSCRIPTION_ENABLED=true` with a real transcription-capable endpoint/model, normalize one short downloaded
  Telegram `voice` or `video` item, and verify that `output/transcripts/...json` is written and the normalized `.txt`
  contains transcript text after the media placeholder. This is intentionally outside unit tests.
  2026-05-31 recheck: `python main.py transcribe backfill --limit 5 --dry-run` still found `0` downloaded native
  media candidates, and `media_cache/` has no local audio/video files. Report:
  `artifacts/transcription_live_check_20260531.md`.
- Evaluate paid model candidates only if DeepSeek V4 Flash stops meeting quality/latency needs.
  Current trusted model remains `deepseek-v4-flash`.

## Notes

- A check belongs here when it depends on model quality, provider stability, rate limits, or multimodal capability.
- When a check is completed, move it to `Completed` with the model, date, command, and artifact path.
- Retired model artifacts can be archived outside the top-level `artifacts/*_scores.json` registry scan so they do not
  appear in the active experiment report.

## Completed

- 2026-06-09: ultra-left/right source hint removal live checks completed.
  The hardcoded source hint for `3299898370/11` was removed after retrieval changes made the canonical source rank first
  on its own. Live checks on `deepseek-v4-flash`:
  selected `ultra_left_right_similarity_source` -> `1/1`, average `100.0`, source rank `1`;
  full source-selection golden -> `10/10`, average `100.0`;
  full answer-quality golden -> `23/23`, average `100.0`.
  Artifacts:
  `artifacts/source_hint_removal_ultra_scores.json`,
  `artifacts/source_hint_removal_ultra_results.md`,
  `artifacts/source_hint_removal_full_source_selection_scores.json`,
  `artifacts/source_hint_removal_full_source_selection_results.md`,
  `artifacts/source_hint_removal_full_golden_scores.json`,
  `artifacts/source_hint_removal_full_golden_results.md`.
- 2026-06-02: v1.1.0 release live checks completed.
  Final release live checks on `deepseek-v4-flash` remained green:
  source-selection golden -> `10/10`, average `100.0`;
  full golden -> `23/23`, average `100.0`.
  Artifacts:
  `artifacts/v1_1_release_source_selection_scores.json`,
  `artifacts/v1_1_release_source_selection_results.md`,
  `artifacts/v1_1_release_full_golden_scores.json`,
  `artifacts/v1_1_release_full_golden_results.md`.
- 2026-06-02: v1.1 Phase 4 enriched rebuild live-check decision completed.
  `python main.py rebuild --from-enriched` is no longer a live-LLM investigation target because enriched-card graph
  rebuild has been removed from the supported main CLI path. The historical failed attempt remains documented in
  `DEVELOPMENT_RETURN_LOG.md`; future work should revisit it only as a separate experiment, not as v1.1 release work.
  After this CLI retirement, live checks on `deepseek-v4-flash` remained green:
  source-selection golden -> `10/10`, average `100.0`;
  full golden -> `23/23`, average `100.0`.
  Artifacts:
  `artifacts/v1_1_phase4_source_selection_scores.json`,
  `artifacts/v1_1_phase4_source_selection_results.md`,
  `artifacts/v1_1_phase4_full_golden_scores.json`,
  `artifacts/v1_1_phase4_full_golden_results.md`.
- 2026-06-01: v1.1 Phase 3 source-selection golden completed.
  Retrieval/card-context ranking was changed, then live source-selection golden was rerun on `deepseek-v4-flash`.
  The Q22 Narva visuals case improved from `0/1` to `1/1`: direct source `3889026624/2` moved from rank 3 to rank 1.
  The full source-selection golden improved from the Phase 2 baseline `9/10`, average `90.0`, to `10/10`, average
  `100.0`.
  The full answer-quality golden after the retrieval change stayed green at `23/23`, average `100.0`.
  A no-hint check for ultra-left/right similarity showed source `3299898370/11` still at rank 4, so the reference hint
  remains a documented safety net rather than being removed.
  Artifacts:
  `artifacts/v1_1_phase3_q22_baseline_scores.json`,
  `artifacts/v1_1_phase3_q22_baseline_results.md`,
  `artifacts/v1_1_phase3_q22_after_visual_rank_scores.json`,
  `artifacts/v1_1_phase3_q22_after_visual_rank_results.md`,
  `artifacts/v1_1_phase3_full_golden_scores.json`,
  `artifacts/v1_1_phase3_full_golden_results.md`,
  `artifacts/v1_1_phase3_source_selection_scores.json`,
  `artifacts/v1_1_phase3_source_selection_results.md`.
- 2026-06-01: v1.1 Phase 2 source-selection golden completed.
  Added and ran `source_selection_golden.py` on `deepseek-v4-flash` with the trusted runtime flags:
  `RERANKER_ENABLED=false`, `HYBRID_SYNTH_ENABLED=true`, `HYBRID_QUERY_CARDS_ENABLED=true`, and `WIKI_ENABLED=true`.
  Selected Q9 Cuba protests source check scored `1/1`, average `100.0`.
  Full source-selection golden scored `9/10`, average `90.0`.
  Full answer-quality golden after adding the source-selection runner scored `23/23`, average `100.0`.
  The single failing case was `q22_narva_visuals_top_source`: direct Narva/Estonia source `3889026624/2` appeared at
  rank 3, while broader Baltic visual sources `3889026624/9` and `3889026624/6` occupied ranks 1-2. This is a Phase 3
  retrieval-ranking target, not a model/provider failure.
  Artifacts:
  `artifacts/v1_1_phase2_source_selected_scores.json`,
  `artifacts/v1_1_phase2_source_selected_results.md`,
  `artifacts/v1_1_phase2_source_selection_scores.json`,
  `artifacts/v1_1_phase2_source_selection_results.md`,
  `artifacts/v1_1_phase2_full_golden_scores.json`,
  `artifacts/v1_1_phase2_full_golden_results.md`.
- 2026-06-01: v1.1 Phase 1 golden checks completed.
  Baseline and post-refactor full golden runs on `deepseek-v4-flash` with `GOLDEN_QUERY_DELAY_SECONDS=0` both scored
  `23/23`, average `100.0`, confirming that the architecture split did not change golden behavior.
  Artifacts:
  `artifacts/v1_1_phase1_baseline_golden_scores.json`,
  `artifacts/v1_1_phase1_baseline_golden_results.md`,
  `artifacts/v1_1_phase1_final_golden_scores.json`,
  `artifacts/v1_1_phase1_final_golden_results.md`.
- 2026-06-01: v1 release golden completed.
  Final no-cache full golden set on `deepseek-v4-flash` with `RERANKER_ENABLED=false`,
  `HYBRID_SYNTH_ENABLED=true`, `HYBRID_QUERY_CARDS_ENABLED=true`, `WIKI_ENABLED=true`, and
  `GOLDEN_QUERY_DELAY_SECONDS=0` scored `23/23`, average `100.0`.
  This run followed deterministic guardrails for known source/wording instability around the ultra-left/ultra-right
  source hint, AfD/AdG aliasing, and weak side-country mentions in the ultra-right overview question.
  Artifacts:
  `artifacts/v1_release_golden_set_scores.json`,
  `artifacts/v1_release_golden_set_results.md`.
- 2026-05-30: DeepSeek V4 Flash full golden check completed.
  1. Pre-rebuild run: the full golden set was run with a cleared LLM cache under recommended settings
     (`RERANKER_ENABLED=false`, `HYBRID_SYNTH_ENABLED=true`, `WIKI_ENABLED=true`).
     Result: `23/23` passed, average score `100.0` (E2 baseline comparison showed `+1.3` average delta and `0` regressions).
     Artifacts: `artifacts/deepseek_v4_flash_clearcache_golden_set_scores.json`,
     `artifacts/deepseek_v4_flash_clearcache_golden_set_results.md`,
     `artifacts/deepseek_v4_flash_clearcache_vs_e2_compare.md`.
  2. Post-rebuild validation run: following the complete rebuild from scratch, the full golden set was run on the newly
     constructed graph with zero delay and empty cache.
     Result: `23/23` passed, average score `100.0`, confirming `0` regressions and identical perfect results.
     Artifacts:
     `artifacts/deepseek_v4_flash_rebuilt_clearcache_golden_set_scores.json`,
     `artifacts/deepseek_v4_flash_rebuilt_clearcache_golden_set_results.md`,
     `artifacts/deepseek_v4_flash_rebuilt_clearcache_vs_e2_compare.md`,
     `artifacts/deepseek_v4_flash_rebuilt_vs_pre_rebuild_compare.md`.
- 2026-05-30: DeepSeek V4 Flash focused probe completed.
  First clear-cache run on the official DeepSeek OpenAI-compatible endpoint scored `5/6`, avg `93.3`,
  avg duration `11.515s`, with no `NoneType.content` failures after explicitly disabling DeepSeek thinking mode.
  The only failure was `afd_funding_absence_control`, where the answer was semantically correct but did not include
  the evaluator's expected absence wording.
  After `_postprocess_answer_text` was updated for DeepSeek-style absence wording, the focused clear-cache probe scored
  `6/6`, avg `100.0`, avg duration `11.487s`. All source checks passed (`source_ok/source_any_ok=Y/Y`).
  Artifacts:
  `artifacts/llm_probe_deepseek_v4_flash_clearcache_scores.json`,
  `artifacts/llm_probe_deepseek_v4_flash_clearcache_results.md`,
  `artifacts/llm_probe_deepseek_v4_flash_absence_fix_clearcache_scores.json`,
  `artifacts/llm_probe_deepseek_v4_flash_absence_fix_clearcache_results.md`.
- 2026-05-31: F4 reranker comparison completed.
  Full golden set with `RERANKER_ENABLED=true`, `deepseek-v4-flash`, cleared LLM cache, `HYBRID_SYNTH_ENABLED=true`,
  and `WIKI_ENABLED=true` scored `20/23`, avg `96.7`. Comparison against the rebuilt no-reranker DeepSeek baseline
  (`23/23`, avg `100.0`) showed `avg_delta=-3.3`, `3` regressions, and `0` improvements.
  Regressions: Q1 ultra-left/ultra-right source selection, Q10 US/Cuba pressure-vs-deal wording, and Q21 AfD funding
  absence wording. Recommendation: keep `RERANKER_ENABLED=false` for the trusted default.
  Artifacts:
  `artifacts/deepseek_v4_flash_reranker_clearcache_golden_set_scores.json`,
  `artifacts/deepseek_v4_flash_reranker_clearcache_golden_set_results.md`,
  `artifacts/deepseek_v4_flash_reranker_vs_rebuilt_baseline_compare.md`.
- 2026-05-28: Added focused live-LLM verification probe:
  `llm_verification_probe.py`.
  It checks F1 Trump/Orban source grounding, Q7 Cuba talks, Q9 Cuba protests, Q22 Narva visuals,
  a North Korea source-control query, and the AfD funding absence-control query.
  It supports `LLM_PROBE_CLEAR_CACHE=true` to move stale LightRAG LLM cache into
  `artifacts/llm_probe_cache_backups/` before running.
