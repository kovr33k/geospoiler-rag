# Текущее полное ревью — 2026-08-13

## Вердикт

`BLOCKED`. Кодовые gates зелёные, но release/production acceptance не закрыта.

## Что проверено

- branch: `work/rag-v2-snapshot-20260810`;
- checkout dirty: 37 tracked files changed и значительный набор untracked source/tests/docs;
- Ruff: clean;
- `git diff --check`: без ошибок, только стандартные CRLF warnings;
- targeted regression: 24 passed, 1 skipped, 3 subtests;
- security scan `artifacts/`: 0 files / 0 occurrences;
- canonical historical backup validator: 408/408 passed, mapping=408, nested/errors=0;
- live egress и production backfill в этом review не запускались.

## Findings

### P1 — media contract не синхронизирован

В production сейчас 95 downloaded media (72 image, 23 video): visual success 6/95, STT transcribed 2/23, failed 21/23, status absent для остальных. Join sidecar→card показывает 103 mismatch: 20 failed-STT без правильного flag/issue и 83 visual non-success без issue. Это нарушает README-инвариант «media failure явно отражается в review condition» и делает часть карточек stale.

### P1 — graph provenance требует детерминированной сверки

LightRAG содержит 129 processed records, но 52 обычных basename paths, 2 YouTube basename paths и 75 virtual paths. Строгий resolver запрещает basename lookup. Нужна проверка 129/129 без ambiguous/missing, иначе graph acceptance нельзя считать доказанной.

### P1 — внешняя credential rotation не подтверждена

Артефакты после redaction: 62 файла/76 совпадений удалены, quarantine SHA проверены, текущий scan зелёный. Но `RISK-TOKEN-ROTATION-001` остаётся `external_action_required`; локальная redaction не доказывает revoke/rotate у issuer.

### P1 — live egress можно запустить слишком легко

`media-backfill --all/--limit` не требует отдельного execute/authorization gate; `--dry-run` opt-in. До изменения контракта обычный операторский вызов может отправить media во внешний provider.

### P1/P2 — ошибки media/backfill недостаточно аудируемы

Visual/transcription backfill сохраняет sidecar в основном на success/transcribed; failed/unavailable terminal status может не попасть в sidecar. Pair-write двух файлов последовательный, без journal/recovery. Новые visual paths/artifacts всё ещё могут быть absolute.

### P1/P2 — restore и smoke gates могут быть недостоверны

`luna_reenrich_control.py` записывает smoke PASS без гарантии фактического smoke artifact. Restore должен валидировать backup manifest/SHA до перемещения active targets; иначе повреждённый backup может оставить target удалённым. Текущий backup manifest и generic restore validator требуют единой schema.

### P2 — stale audit/docs/release evidence

`scripts/luna_reenrich_audit.py` сохраняет исторические expected 131/135 и конфликтует с динамическим corpus 129. Старые closeout reports не являются evidence текущего dirty checkout. Нужно создать новый immutable final verification report после последних code/data/doc изменений. README должен содержать безопасный media-backfill runbook.

### P1/P2 — path traversal hardening

Portable media helpers и resolvers должны отклонять `..`, symlink/junction escape и абсолютные пути вне разрешённого media root. Иначе crafted sidecar может направить backfill на произвольный локальный файл.

## ТЗ на исправление

Полное ТЗ находится в:

- `POST_PUSH_REVIEW_CURRENT_TZ_20260813.md`;
- `POST_PUSH_REVIEW_CURRENT_TZ_ADDENDUM_20260813.md`;
- `POST_PUSH_REVIEW_CURRENT_TZ_ADDENDUM2_20260813.md`;
- `POST_PUSH_REVIEW_CURRENT_TZ_ADDENDUM3_20260813.md`;
- `POST_PUSH_REVIEW_CURRENT_TZ_ADDENDUM4_20260813.md`.

Основной scope — wide acceptance 95/95; scoped Ultra допускается только отдельным письменным решением и не закрывает global media DoD.

## Что не выполнено в рамках ревью

Не запускались live STT/visual backfill, full re-enrichment, новые LightRAG writes и issuer-side credential rotation. Поэтому текущий verdict остаётся `BLOCKED`.
