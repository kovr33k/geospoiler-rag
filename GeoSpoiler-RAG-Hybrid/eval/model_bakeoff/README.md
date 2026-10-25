# Model Bakeoff

This package runs read-only LLM model bakeoffs for GeoSpoiler-RAG.

It is intentionally separate from production RAG storage:

- no LightRAG rebuild;
- no retrieval/reranker/wiki changes;
- no writes outside `artifacts/model_bakeoff/{run_id}`;
- deterministic scoring first, optional judge later.

## Smoke

Dry run:

```powershell
python -m eval.model_bakeoff.run_bakeoff --suite eval/model_bakeoff/suites/chinese_political_risk.jsonl --limit 2 --dry-run
```

Chinese risk smoke:

```powershell
python -m eval.model_bakeoff.run_bakeoff --suite eval/model_bakeoff/suites/chinese_political_risk.jsonl --families chinese --limit 5
```

Western quality smoke:

```powershell
python -m eval.model_bakeoff.run_bakeoff --suite eval/model_bakeoff/suites/western_quality_enrichment.jsonl --suite eval/model_bakeoff/suites/western_quality_translation.jsonl --families western --limit 5
```

## API Keys

DeepSeek direct models use `DEEPSEEK_API_KEY` or the existing `LLM_API_KEY`.
OpenRouter models use `OPENROUTER_API_KEY`.

## Outputs

Each run writes:

- `config_snapshot.json`
- `model_outputs/{model_id}/{case_id}.json`
- `scores/political_risk_scores.csv`
- `scores/quality_scores.csv`
- `scores/role_recommendations.json`
- `report.md`
- `failures.md`
