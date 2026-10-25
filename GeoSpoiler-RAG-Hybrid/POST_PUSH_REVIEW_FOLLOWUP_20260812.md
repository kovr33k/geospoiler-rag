# ТЗ: закрытие follow-up findings после post-push closeout review

Проект: `D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid`
Дата: 2026-08-12
Основание: `artifacts/closeout_followup_review_20260812.md`

Документ только задаёт исправления. Commit/push, массовая перегенерация и
ротация credential этим ТЗ не выполняются автоматически.

## 1. Цель и решение scope

Перед изменениями зафиксировать один из двух режимов:

1. **Corpus-wide media acceptance:** все 95 media-объектов входят в DoD; тогда
   повторить STT/visual backfill с checkpoint/resume и закрыть 21 STT failure и
   89 отсутствующих visual evidence либо поместить каждый объект в явный
   quarantine/review с причиной.
2. **Scoped Ultra acceptance:** production DoD распространяется только на четыре
   Ultra-карточки; остальные 91 media-объект явно исключаются из acceptance и
   получают отдельный backlog/report. В этом режиме нельзя писать «media corpus
   полностью закрыт».

Без выбранного режима verdict `READY_FOR_COMMIT` запрещён.

## 2. P1 — credential safety

1. Зафиксировать SHA-инвентарь `.env` только локально, без вывода значений.
2. Удалить из локального runtime неиспользуемые reranker и legacy provider keys;
   `RERANKER_ENABLED=false` должно сопровождаться пустым `RERANKER_API_KEY`.
3. У владельца провайдера выполнить revoke/rotate всех credential, которые могли
   попасть в `.env`, logs, artifacts или quarantine. Ротация redaction-ом не
   заменяется.
4. Получить машинно проверяемое подтверждение issuer-side rotation/revoke и
   обновить `RISK-TOKEN-ROTATION-001` на `resolved` с датой и evidence.
5. Повторить scan без вывода секретов в трёх областях: tracked checkout,
   `artifacts/`, publishable `output/` + active registry. Synthetic fixtures
   допускаются только по явному path allowlist и должны быть отмечены как test
   data.

**Gate:** нет live-looking credential вне намеренно изолированного quarantine;
rotation evidence приложено; `.env` не tracked.

## 3. P1/P2 — media/STT and sidecar/card contract

1. Реализовать выбранный в разделе 1 scope; не запускать массовый pipeline без
   immutable backup, manifest и checkpoint.
2. Для каждого media item сохранять `input_sha`, provider/model identity,
   status, error class, retry count и artifact path.
3. Для OpenRouter STT проверить контракт multipart: model, field name, MIME,
   size/duration limits и fallback. Отдельно воспроизвести 400 и premature
   response на synthetic fixture; не считать caption-only карточку успешной
   media extraction.
4. Для visual backfill сохранить `success|failed|unavailable` sidecar даже при
   отрицательном результате; повторный запуск не должен повторять неизменный
   identity без явного retry policy.
5. Выполнить deterministic join normalized sidecars → enriched cards. Каждый
   `transcription_status=failed` обязан давать `transcript_unavailable` и
   `extraction_issues` с media error; каждый media item без visual success обязан
   давать `visual_missing` issue либо быть явно scoped out. Сейчас подтверждено:
   21 failed STT, из них 19 карточек без соответствующего flag/issue; 89 media
   items без visual success, из них 61 карточка без `media:*:visual_missing`.
6. При mismatch fingerprint не патчить JSON вручную: повторно собрать карточку
   штатным pipeline либо quarantine с причиной.
7. После backfill атомарно rebuild normalized/enriched, FTS и source registry;
   LightRAG загружать только через accepted manifest и terminal `processed`.

**Gate (wide):** 95/95 имеют финальный auditable status, 0 silent fallback,
ошибки классифицированы; STT/visual success или explicit quarantine/review для
каждого объекта. **Gate (scoped):** четыре Ultra-карточки 4/4 PASS, остальные
явно исключены из DoD, а sidecar/card join по ним не должен создавать ложный
global closeout.

## 4. P2 — canonical documentation

1. В `POST_PUSH_REVIEW_REMEDIATION_TZ.md` заменить hard-coded `2/2` на
   `expected == len(manifest.sources)` и ссылку на текущий 4/4 report.
2. Все `LUNA_GEMINI_*` документы разделить на `historical` и `current status`;
   убрать формулировки «исправления выполняются» из документов, которые читаются
   как текущая acceptance-инструкция.
3. Один canonical status document должен содержать flags, scope, counts,
   manifest path, report paths и дату следующего review.
4. Любой финальный report должен выдавать `BLOCKED`, если P1 external action или
   media scope не закрыт; `READY_FOR_COMMIT` разрешён только после всех gates.

## 5. P2 — backup/restore hardening

1. Расширить validator до схемы canonical path mapping: разрешены только
   `output/...`, `artifacts/...`, `rag_storage/...`; mapping обязан быть
   однозначным и не содержать повторного namespace/channel компонента.
2. Добавить тесты для `output/enriched/<channel>/<channel>/x`,
   `rag_storage/rag_storage/x`, path traversal, symlink escape и duplicate target.
3. Для legacy backups либо создать canonical copy, либо пометить manifest
   `legacy_restore_only`; не объявлять dry-run полноценным restore без mapping.
4. Проверять SHA до и после isolated restore, SQLite `integrity_check` и число
   восстановленных файлов.

**Gate:** synthetic ambiguous layouts fail-closed; canonical 408/408 остаётся
зелёным; restore report содержит mapping version.

## 6. P2 — release hygiene

1. Добавить `.pytest-*` и аналогичные локальные basetemp-пути в `.gitignore` либо
   всегда использовать basetemp вне checkout.
2. Перед commit пересоздать baseline после всех правок и получить SHA для code,
   manifest, output, active DB и reports.
3. Разделить generated artifacts, publishable reports и tracked source/tests;
   удалить только проверенные временные каталоги, не трогая production data.
4. Провести diff review по всем 32 tracked changes и untracked source/tests;
   добавить только намеренные файлы в один reviewable commit.
5. Повторить `git diff --check`, Ruff, полный pytest и enriched validator после
   staging; push остаётся отдельным действием.

## 7. Обязательные тесты и evidence

### Unit/regression

- STT 400, premature response, multipart/model fallback и size/duration limits;
- media status persistence, retry idempotence и caption-only guard;
- visual success/failure/unavailable с input SHA/model identity;
- sidecar/card join: failed STT → transcript flag, missing visual → issue;
- full-repo token scan с synthetic-fixture allowlist;
- backup validator для повторных channel namespaces, traversal/symlink/duplicate;
- manifest dynamic N/N и LightRAG missing/nonterminal status.

### Required commands

```powershell
Set-Location D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q --basetemp D:\GeoSpoiler-test-tmp\followup
.venv\Scripts\python.exe main.py validate enriched --fail-on-error
python scripts\security_artifact_redaction.py scan --root artifacts
python scripts\validate_backup_restore.py --manifest <canonical> --report <report>
git diff --check
```

## 8. Финальный DoD

Работа закрыта только если одновременно выполнены:

1. `RISK-TOKEN-ROTATION-001` resolved с внешним evidence, либо release остаётся
   `BLOCKED`.
2. Media scope явно выбран и выполнен: wide 95/95 auditable или scoped 4/4 с
   явным исключением остальных материалов.
3. Sidecar/card join не имеет необъяснимых failed-media рассинхронизаций;
   `partial_segment_failure=0`, technical placeholders в semantic fields=0,
   provenance/URL/citations валидны.
4. Manifest dynamic `expected == len(sources) == inserted`; processing/failed=0.
5. FTS/registry/graph согласованы; stale China/6 и China/72 отсутствуют.
6. Token scan publishable scope=0; synthetic test data не маскирует live leak.
7. Canonical backup/restore fail-closed и SHA/integrity gates зелёные.
8. Документация не содержит конфликтующих `2/2` и stale current wording.
9. Full pytest, Ruff, enriched validator и diff check зелёные после нового
   baseline.
10. Final review report содержит branch/HEAD, dirty diff inventory, scope,
    evidence, residual risks и один из verdict: `READY_FOR_COMMIT` или `BLOCKED`.
