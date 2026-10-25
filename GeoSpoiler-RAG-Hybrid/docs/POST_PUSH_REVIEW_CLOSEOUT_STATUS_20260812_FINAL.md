# Current post-push closeout status — 2026-08-12

Этот документ — актуальный status overlay для исторических canary/pipeline/execution отчётов. Старые документы сохраняют исходные evidence и не являются текущим acceptance verdict.

## Current runtime and data

- Luna text/enrichment; Gemini/OpenRouter STT.
- `LATE_FUSION_ENABLED=true`.
- `RERANKER_ENABLED=false`.
- Enriched validation: 131/131 valid, errors=0, warnings=0.
- Accepted manifest contract is dynamic: `expected == len(sources)`, current remediation load 4/4.
- LightRAG: 129 records processed, processing=0, failed=0; China/6 and China/72 stale records absent.
- FTS: cards=131, YouTube segments=22, Wiki=160.
- Registry: sources=133, normalized=129, enriched=131, references=32.

## Security and recovery

- Artifact redaction: 62 historical files, 0 unredacted findings after remediation.
- All 62 original files are outside the repository in SHA-verified quarantine.
- Sanitized backup restore dry-run: 408/408 files passed size/SHA validation.
- `RISK-TOKEN-ROTATION-001` is `external_action_required` until the credential owner confirms rotation/revocation by 2026-08-19.

## Release status

Full pytest: 652 passed, 2 warnings; Ruff passed. The code/data state is `READY_FOR_COMMIT`. Commit and push are separate operator actions. Do not stage the external quarantine or any raw-token material.
