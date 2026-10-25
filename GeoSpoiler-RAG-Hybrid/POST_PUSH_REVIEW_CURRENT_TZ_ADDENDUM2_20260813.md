# Addendum 2 к ТЗ текущего review

Обязателен вместе с `POST_PUSH_REVIEW_CURRENT_TZ_20260813.md` и его addendum.

1. `scripts/luna_reenrich_control.py restore` должен до любого `shutil.move` прочитать `backup_manifest.json`, проверить path allowlist, existence, SHA/size и SQLite integrity. При ошибке активный target нельзя перемещать; нужен preflight и rollback on copy failure.
2. README и canonical runbook должны описывать `media-backfill`: dry-run по умолчанию, explicit live authorization, selectors, report path и recovery procedure.
3. `normalizer/transcription_backfill.py` должен обрабатывать malformed selector (`abc`, `None`, отрицательный ID) через Optional-safe helper до вызова `int()`. Один плохой ID не должен прерывать весь backfill.

Acceptance: synthetic corrupted/missing backup не меняет active target; malformed selector даёт machine-readable skip; operator docs содержат полный безопасный runbook.
