# ТЗ: финальное закрытие замечаний post-push review

**Проект:** `D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid`
**Дата:** 2026-08-12
**База:** `2c3d749eec2526120d651cf6e8d4b910d8c9d4bf`
**Ветка:** `work/rag-v2-snapshot-20260810`
**Статус:** к исполнению. Исполнение, commit и push этим документом не выполняются.

## 1. Цель и неизменяемые границы

Довести состояние до `READY_FOR_COMMIT`, закрыв все замечания последнего ревью. Сохраняются: Luna для текста/enrichment, Gemini/OpenRouter для STT, `LATE_FUSION_ENABLED=true`, `RERANKER_ENABLED=false`, accepted-manifest load только fail-closed. Push в GitHub — отдельное явное действие после commit.

Каждая production-операция начинается с SHA-256 backup и machine-readable manifest. Нельзя использовать `git reset --hard`, force-push, прямое редактирование LightRAG JSON или принимать partial result.

## 2. Findings, которые нужно закрыть

### F1 — token-like URLs в artifacts/backups (P1 security/DoD)

Read-only scan ранее нашёл **62 файла** в `artifacts/` с необезличенными credential-like query values. Это были текст, JSON, логи и SQLite-копии; старые backup/canary-копии вынесены во внешнее quarantine. Текущие publishable `output` и active registry замаскированы.

Сначала зафиксировать inventory/SHA и retention decision. Если значение могло быть действующим credential, отдельно выполнить ротацию у владельца источника; redaction не заменяет rotation.

### F2 — manifest/ТЗ drift (P2 contract)

Старое ТЗ фиксирует `expected=2`, но фактический canonical финальный run имеет `expected=4`, `inserted=4`. DoD должен быть динамическим: `expected == len(manifest.sources)` и `inserted == expected`; текущий baseline — 4.

### F3 — accepted risks без owner/deadline (P2 governance)

Для flattened Codex CLI prompt, LightRAG alias warnings и legacy backup layout описаны compensating controls, но отсутствуют `owner`, `due_date`, `mitigation`, `status`, `evidence` и `review/expiry date`.

### F4 — LightRAG auto-merge warnings (P2 operational)

Auto-merge отдельных alias entities дал нефатальные warnings; документы стали `processed`. Каждый warning должен получить disposition: детерминированный verified mapping, исправление входного label с isolated reload или accepted risk. Нельзя добавлять aliases автоматически без canonical evidence.

### F5 — nested backup layout (P2 recovery)

В исторических backup встречаются `rag_storage/rag_storage` и повторные каталоги каналов. Нужен canonical restore layout либо fail-closed restore validator с однозначным mapping и SHA-check до замены.

### F6 — исторические статусы в документации (P2 documentation)

Canary/pipeline/execution документы смешивают исторический текст («исправления выполняются», «массовая обработка не запускалась») с текущим статусом. Историю сохраняем, но current status и final evidence должны быть отдельными разделами.

### F7 — release hygiene

Рабочее дерево намеренно dirty и не собрано в один reviewable commit. До commit требуется зафиксировать полный diff, исключить случайные generated artifacts и повторить все gates.

## 3. Фактические API и шаблоны

Исполнитель использует существующие контракты:

- `normalizer.youtube_handler.redact_sensitive_url(value: str) -> str` (`normalizer/youtube_handler.py:146-156`) — базовая семантика redaction.
- `retrieval.source_registry.rebuild_source_registry(...)` (`retrieval/source_registry.py:60-169`) и `python main.py registry rebuild` — штатный rebuild active registry.
- `loader.ingest.load_from_manifest(rag, manifest_path)` (`loader/ingest.py:432+`) и report writer (`loader/ingest.py:66-95`); CLI `python main.py load --manifest ...` (`cli_app.py:69-74`).
- `scripts/luna_reenrich_control.py`: `_sha256` (`:29`), `prepare` (`:173`), `restore` (`:299`), CLI `prepare|activate|restore --run-dir` (`:340-355`) — backup/restore шаблон.
- `loader.storage._cleanup_skipped_doc` (`:206-245`) и `_doc_status_value` (`:248+`) — fail-closed cleanup/status.
- `loader.entity_merge.plan_safe_entity_merges` (`:52-94`) и `auto_fix_safe_entity_merges` (`:97-118`) — только для проверяемых alias mappings.
- README operator rules (`README.md:66-182`, `:344-411`) и Operations (`OPERATIONS.md:347-392`).

Для historical artifacts нет готового recursive scrubber: его нужно добавить с dry-run, manifest, SHA before/after и обработкой SQLite через штатный SQLite API, не байтовой заменой.

## 4. Исполнительные фазы

### Фаза 0 — baseline и inventory

1. Зафиксировать branch/HEAD, flags, configured paths и counts.
2. Создать `artifacts/closeout_baseline_20260812.json` с SHA затрагиваемых файлов.
3. Сканировать `output/`, `rag_storage/`, `artifacts/` по text/JSON/log/SQLite; значения токенов не выводить в отчёт.
4. Составить inventory legacy nesting, docs drift и LightRAG warnings.

Gate: production SHA до изменений совпадают с baseline; при ошибке остановиться.

### Фаза 1 — token redaction/quarantine

1. Зафиксировать inventory и SHA до обработки.
2. Для text/JSON/log применить общий helper на базе `redact_sensitive_url`; значение query заменять на `%5BREDACTED%5D`.
3. Active registry rebuild делать через `rebuild_source_registry`/CLI, с backup и query verification. SQLite-копии открывать штатным SQLite API; не делать байтовую замену.
4. Неизменяемые исходные копии поместить в явно обозначенное защищённое quarantine либо удалить только после утверждённого retention decision.
5. Записать `before_sha256`, `after_sha256`, `redacted_count`, `retention_class`, `rotation_status`.

Gate: `unredacted_access_token_count=0` в publishable output, active registry, retained backups и SQLite; synthetic test fixtures разрешены только с явной маркировкой.

### Фаза 2 — canonical backup/restore

1. Canonical layout: `backup_root/manifest.json` + пути относительно project root (`output/...`, `artifacts/...`, `rag_storage/...`), без повторного `output`/`rag_storage`.
2. Добавить validator/restore wrapper, который обнаруживает legacy nesting, строит однозначную mapping-таблицу, запрещает ambiguity и проверяет SHA до/после dry-run.
3. Legacy artifacts не переписывать молча: создать sanitized canonical copy либо пометить `legacy_restore_only`.

Gate: изолированный dry-run restore восстанавливает 100% файлов в ожидаемые пути с теми же SHA.

### Фаза 3 — manifest и документация

1. В master remediation-ТЗ заменить `expected=2` на `expected == len(manifest.sources)`; записать текущий `4/4`.
2. Синхронизировать README/runbook с canonical manifest, exact Windows provenance paths, active DB paths, backup/load/rollback и no-reranker policy.
3. Разделить historical/current в `LUNA_GEMINI_CANARY_REPORT.md`, `LUNA_GEMINI_MEDIA_PIPELINE_PLAN.md`, `LUNA_GEMINI_MEDIA_EXECUTION_REPORT.md`.
4. Создать `artifacts/accepted_risk_register_20260812.json` со schema:
   `id`, `finding`, `severity`, `owner`, `due_date`, `mitigation`, `status`, `evidence`, `review_or_expiry_date`.

Gate: нет contradictory expected/count/status; ни один accepted risk не имеет пустого owner/deadline/evidence.

### Фаза 4 — LightRAG warning disposition

1. Сгруппировать warnings по entity/source/doc без изменения production.
2. Для каждого выбрать verified deterministic mapping, isolated data fix/reload или accepted risk.
3. Добавить regression test: warnings не повышают `failed`, не оставляют `processing` и не скрывают partial insert.
4. Повторить manifest load только через `python main.py load --manifest <canonical-path>`; N/N — единственный accepted.

Gate: все warnings классифицированы; `processed` у всех текущих документов, `processing=0`, `failed=0`, `inserted==expected`.

### Фаза 5 — release verification

```powershell
Set-Location D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q --basetemp D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid\.pytest-closeout
.venv\Scripts\python.exe main.py validate enriched --fail-on-error
git diff --check
```

Затем проверить: cards `131/131` либо новый count с baseline explanation; invalid URLs=0; `partial_segment_failure=0`; semantic technical placeholders=0; FTS/registry согласованы; stale China/6/72 отсутствуют; active configured DB — `artifacts/card_fts.sqlite` и `artifacts/source_registry.sqlite`; token scan=0.

## 5. Финальный DoD

Работа закрыта только если одновременно выполнено:

1. `unredacted_access_token_count=0` в output, registry и retained backups; rotation status записан при необходимости.
2. Canonical restore dry-run проходит SHA без ambiguous nesting.
3. Manifest/docs согласованы; `expected == len(sources)`, `inserted == expected`, текущий run `4/4`.
4. LightRAG полностью `processed`, `processing=0`, `failed=0`; China/6 и China/72 отсутствуют; warnings имеют disposition.
5. Enriched validator green, errors=0, warnings=0.
6. `partial_segment_failure=0`; технических placeholders в semantic fields нет. `[Instagram Reel: URL]` в source `text` не считать semantic placeholder.
7. Risk register заполнен owner/deadline/mitigation/evidence для каждого accepted risk.
8. Ruff, полный pytest и diff check green.
9. Final report содержит branch/HEAD, changed files, counts, SHA-manifests, token report, restore report, warning disposition, risk register и список remaining risks.
10. Verdict строго `READY_FOR_COMMIT`; `READY_FOR_PUSH` не выдаётся автоматически.

## 6. Обязательные артефакты

- `artifacts/closeout_baseline_20260812.json`
- `artifacts/token_redaction_closeout_20260812.json`
- `artifacts/backup_restore_closeout_20260812.json`
- `artifacts/lightrag_warning_disposition_20260812.json`
- `artifacts/accepted_risk_register_20260812.json`
- `artifacts/accepted_manifest_load_report.json`
- `artifacts/closeout_test_report_20260812.json`
- `artifacts/closeout_final_review_20260812.md`

При любом failed gate результат не принимается; rollback выполняется только из SHA-проверенного backup.
