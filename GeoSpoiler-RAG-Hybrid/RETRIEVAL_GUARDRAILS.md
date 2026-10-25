# Retrieval Guardrails

This document tracks deterministic source/answer guardrails around the retrieval
pipeline. The goal is to keep them visible, avoid confusing them with model
quality, and remove them only after retrieval itself is strong enough.

## Current Retrieval Changes

The ultra-left/right source hint has been removed. The direct similarity source
`3299898370/11` is now surfaced by retrieval itself:

- thin enriched cards without evidence fields fall back to their normalized
  source text for local card search;
- similarity wording is expanded lexically (`сходство` / `совпад*` /
  related forms) without tying the query to a source id;
- card/source metadata headers are excluded from content-coverage ranking so a
  channel title does not masquerade as evidence;
- `ультралев*` and `ультраправ*` are no longer matched only because they share
  the `ультра` prefix;
- `AfD` / `АдГ` aliases are expanded before card-context ranking;
- card-context reranking uses a wider candidate pool before keeping the final
  small context set.

Verification:

```powershell
SOURCE_GOLDEN_CASE_IDS=ultra_left_right_similarity_source python source_selection_golden.py
python source_selection_golden.py
python tests/test_golden_set.py
```

Artifacts:

- `artifacts/source_hint_removal_ultra_results.md`
- `artifacts/source_hint_removal_ultra_scores.json`
- `artifacts/source_hint_removal_full_source_selection_results.md`
- `artifacts/source_hint_removal_full_source_selection_scores.json`
- `artifacts/source_hint_removal_full_golden_results.md`
- `artifacts/source_hint_removal_full_golden_scores.json`

## Resolved Safety Nets

| Guardrail | Former location | Resolution | Verification |
|---|---|---|---|
| Ultra-left/right similarity source hint | `loader/reference_hints.py` | Removed. Retrieval now ranks canonical source `3299898370/11` at source rank 1 without a hardcoded hint. | Selected source case `1/1`, full source-selection `10/10`, average `100.0`. |

## Active Safety Nets

| Guardrail | Location | Protected failure | Status |
|---|---|---|---|
| AfD / АдГ alias wording | `loader/answer_postprocess.py` | The answer may use only the Russian alias `АдГ`, while golden/user questions often say `AfD`. | Active wording safety net. Retrieval alias expansion now helps source ranking, but answer wording still keeps both forms clear. |
| AfD problematic-party Ukraine marker | `loader/answer_postprocess.py` | Broad AfD/Russia answers can omit the Ukraine-support context that exists in the corpus. | Active answer safety net until synthesis reliably includes the Ukraine evidence unaided. |
| AfD leak proof wording | `loader/answer_postprocess.py` | AfD/Russia leak answers can correctly describe suspicion, but use `доказан*` inside a negated phrase that the golden forbids to prevent overclaiming. | Active narrow answer wording safety net. |
| AfD funding absence wording | `loader/answer_postprocess.py` | Correct absence answers may omit explicit absent / cannot-determine wording. | Active evaluator/user clarity safety net. |
| Ultra-right overview country markers and weak-region cleanup | `loader/answer_postprocess.py` | Overview answers can omit Germany/Russia or over-emphasize weak side mentions like Moldova/Sweden. | Active overview safety net. |
| Trump / European ultraright unsupported hedge cleanup | `loader/answer_postprocess.py` | Model wording can add `якобы` to a Bloomberg-attributed Trump/ultraright answer even when source evidence does not need that hedge. | Active narrow answer wording safety net. |
| Card references before graph references | `loader/card_context.py` | LightRAG graph references can be broad while enriched cards point at direct local evidence. | Kept, but card context is content-ranked and source-selection guarded. |

## Removal Policy

Remove one guardrail at a time only after the relevant source-selection case
passes without it.

Required checks for removal:

```powershell
python -m unittest
python source_selection_golden.py
python tests/test_golden_set.py
```
