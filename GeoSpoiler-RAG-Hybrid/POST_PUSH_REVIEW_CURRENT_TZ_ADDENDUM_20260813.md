# Addendum к ТЗ текущего review — дополнительные блокеры

Этот addendum является обязательной частью `POST_PUSH_REVIEW_CURRENT_TZ_20260813.md`.

## Дополнительные P1/P2 исправления

1. **Безопасный CLI media-backfill.** Сейчас live egress включается обычным вызовом CLI, а `--dry-run` является opt-in. Изменить контракт: dry-run по умолчанию; live только с явным `--execute-live` и machine-readable authorization marker. Обычные `--all`/`--limit` не должны отправлять media наружу.
2. **Честный Luna smoke.** `scripts/luna_reenrich_control.py` не должен записывать `luna_smoke.passed=true` только из метаданных. Требуется фактический smoke artifact или явный `not_run`; preflight не может выдавать PASS за невыполненный smoke.
3. **Dynamic audit.** Убрать из `scripts/luna_reenrich_audit.py` исторические literal expected `131/135` и прочие фиксированные counts, конфликтующие с текущим 129-document corpus. Использовать accounting invariants control script и строгие 4 YouTube source/video IDs.
4. **Отрицательные media-результаты.** `visual_backfill` и `transcription_backfill` обязаны сохранять `failed|unavailable`, error class, route/attempts/latency/input SHA и artifact identity также при ошибке. Нельзя писать sidecar только для success/transcribed.
5. **Portable visual paths.** Все новые visual sidecar/artifact `file_path` и `source` должны проходить `portable_media_path`; старые абсолютные пути не должны появляться после backfill.
6. **Pair-write recovery.** Последовательные `replace` двух файлов не являются атомарной парой. Добавить journal/recovery protocol или доказуемый pair-commit, fault-injection test и recovery report.
7. **Auditable media contract.** `media_contract_version=media-contract-v2` должен быть в card/audit output и проверяться validator-ом; хранить его только в progress state недостаточно.

## Acceptance для этих пунктов

- unit/regression tests покрывают live-gate без фактического egress, smoke `not_run`, failed visual/STT persistence, portable visual paths и crash recovery;
- dry-run не меняет production и не вызывает provider;
- canary report показывает фактический route/attempts/status/latency без секретов;
- final report не использует старый `131/135` baseline;
- после wide backfill 95/95 item-level records имеют финальный статус, а join sidecar→card даёт 0 mismatch.

До выполнения этих пунктов и внешней credential rotation verdict остаётся `BLOCKED`.
