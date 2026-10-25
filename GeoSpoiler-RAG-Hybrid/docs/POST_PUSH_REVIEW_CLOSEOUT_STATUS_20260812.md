# Current post-push closeout status — 2026-08-12

Этот файл является текущим операторским статусом и supersedes неоднозначные
исторические формулировки в старых canary/pipeline/execution отчётах. Историю
первых запусков сохраняем без переписывания.

## Current state

- Luna text/enrichment: active.
- Gemini/OpenRouter STT: active.
- Late-Fusion: enabled.
- Reranker: disabled (`RERANKER_ENABLED=false`).
- Enriched validation: 131/131 valid.
- Final accepted manifest: dynamic `expected == len(sources)`, current run 4/4.
- LightRAG: current documents processed; alias warnings are non-blocking and
  dispositioned in `artifacts/lightrag_warning_disposition_20260812.json`.
- Backup restore: SHA dry-run passed for 408/408 files;
  `artifacts/backup_restore_closeout_20260812.json`.

## Remaining closeout gate

Security artifact redaction must finish with zero unredacted token-like values
in retained artifacts/backups. The accepted risks and their owners/deadlines
are recorded in `artifacts/accepted_risk_register_20260812.json`.

The project is not `READY_FOR_PUSH` until the final verification report is
green. Commit and push remain separate operator actions.
