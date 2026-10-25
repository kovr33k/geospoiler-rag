# ТЗ: закрытие остаточных замечаний финального post-push review

**Проект:** `D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid`
**Дата:** 2026-08-12
**Базовый commit:** `2c3d749eec2526120d651cf6e8d4b910d8c9d4bf`
**Ветка:** `work/rag-v2-snapshot-20260810`
**Статус:** к исполнению; этот документ не является подтверждением готовности к commit/push.

## 1. Цель

Закрыть все остаточные замечания последнего независимого ревью и довести проект до проверяемого состояния `READY_FOR_COMMIT` без изменения согласованной архитектуры:

- текст и enrichment — Luna;
- STT — Gemini/OpenRouter;
- `LATE_FUSION_ENABLED=true`;
- `RERANKER_ENABLED=false`;
- accepted-manifest загрузка — только fail-closed;
- production data меняются только через SHA-проверенный backup, атомарную замену и post-check.

Push в GitHub в это ТЗ не входит и выполняется только отдельным явным действием после commit.

## 2. Зафиксированное состояние и найденные проблемы

На момент ревью подтверждено: `649 passed`, Ruff green, Enriched `131/131`, accepted manifest `4/4`, LightRAG `processing=0/failed=0`, `partial_segment_failure=0`, stale China/6 и China/72 отсутствуют.

Остались следующие задачи:

### F1 — незамаскированные token-like URL в исторических artifacts/backups (P1 security/DoD)

В `artifacts/` ранее находились файлы с необезличенными credential-like query values. В текущих publishable `output` и active registry значения замаскированы; старые backup/canary-копии вынесены во внешнее quarantine и не входят в публикацию.

Нельзя просто удалить файлы без инвентаря: сначала зафиксировать пути, SHA и решение по retention. Если значение было действующим credential, выполнить ротацию у владельца источника; redaction не считается ротацией.

### F2 — расхождение manifest/ТЗ по числу источников (P2 contract/documentation)

В старом ТЗ зафиксировано `expected=2`, а фактический финальный запуск выполнен по manifest с `expected=4`, `inserted=4`. Для текущего remediation canonical является именно финальный manifest с четырьмя источниками. Фиксированное число в DoD заменить на `expected == len(manifest.sources)` и отдельно записать текущий baseline `4`.

### F3 — accepted risks без владельца и срока (P2 governance)

В финальном отчёте описаны compensating controls для flattened Codex CLI prompt, LightRAG auto-merge warnings и nested backup layout, но отсутствуют обязательные `owner`, `due_date`, `mitigation`, `status`. Без этих полей риск не считается formally accepted.

### F4 — нефатальные LightRAG auto-merge warnings (P2 operational)

Во время финальной загрузки были предупреждения о невозможности auto-merge отдельных alias entities. Документы достигли `processed`, поэтому это не failure загрузки, но предупреждения должны быть либо устранены детерминированной картой alias, либо зарегистрированы как accepted operational risk. Нельзя добавлять aliases автоматически без проверяемого canonical mapping.

### F5 — неоднозначная nested backup-структура (P2 recovery)

Исторические backup-копии содержат лишние уровни каталогов (`rag_storage/rag_storage`, повторный каталог канала в `output`). SHA проверяются, но оператор может восстановить не тот путь. Нужен canonical restore layout либо fail-closed restore tool, который явно распознаёт legacy nesting и проверяет SHA до замены.

### F6 — устаревшие статусы в документации (P2 documentation)

`LUNA_GEMINI_CANARY_REPORT.md`, `LUNA_GEMINI_MEDIA_PIPELINE_PLAN.md` и `LUNA_GEMINI_MEDIA_EXECUTION_REPORT.md` содержат исторические формулировки «исправления выполняются»/«массовая обработка ещё не запускалась». История должна быть сохранена, но текущий статус и ссылка на final reports должны быть однозначно отделены.

### F7 — release hygiene

Рабочее дерево намеренно dirty: изменения ещё не собраны в один reviewable commit. Это не дефект runtime, но final gate обязан зафиксировать полный список файлов, исключить случайные generated artifacts и разрешить commit только после прохождения всех gates.

## 3. Допустимые существующие API и шаблоны

Исполнитель обязан опираться на существующие реализации, а не изобретать параллельный контракт:

1. `normalizer.youtube_handler.redact_sensitive_url` — текущая семантика маскирования URL (`normalizer/youtube_handler.py:146`). Общий redaction helper вынести/переиспользовать, не дублировать несовместимые regex.
2. `scripts/luna_reenrich_control.py:_sha256`, `prepare`, `restore` (`:29`, `:173`, `:299`) — шаблон SHA-manifest, проверки backup и подтверждённого restore.
3. `loader.ingest.load_from_manifest` (`loader/ingest.py:432`) — единственная точка accepted-manifest preflight/load.
4. `loader.storage._cleanup_skipped_doc` и `_doc_status_value` (`loader/storage.py:206`, `:248`) — fail-closed cleanup и нормализация статуса LightRAG.
5. `loader.entity_merge` — текущая логика alias auto-merge (`loader/entity_merge.py:58-112`); новые aliases добавлять только через проверяемую детерминированную конфигурацию.
6. Operator backup/load правила README (`README.md:66-182`, `:344-411`) и `OPERATIONS.md:347-392`.
7. Текущее remediation-ТЗ — инварианты, тестовые команды и DoD (`POST_PUSH_REVIEW_REMEDIATION_TZ.md:160-204`).

Антипаттерны: `git reset --hard`, force-push, массовое удаление без manifest, прямое редактирование LightRAG JSON в обход API, принятие partial load, хранение сырых токенов в новых backup и hard-coded `expected=2`.

## 4. План исполнения

### Фаза 0. Baseline и инвентарь

1. Зафиксировать branch/HEAD, активные flags, configured paths (`artifacts/card_fts.sqlite`, `artifacts/source_registry.sqlite`, `rag_storage`) и текущие counts.
2. Сохранить machine-readable baseline с SHA для файлов, которые будут затронуты.
3. Выполнить read-only token scan по `output/`, `rag_storage/`, `artifacts/` и отдельно отметить encrypted/off-workspace quarantine, если он используется.
4. Составить список legacy backup nesting и список всех документов с устаревшим статусом.

**Проверка:** baseline manifest читается, SHA воспроизводятся, production corpus не изменился.

### Фаза 1. Безопасная очистка token-like artifacts

1. Создать backup самого inventory и SHA до redaction; исходные копии с потенциальными credential переместить только в защищённое quarantine с явным retention decision.
2. Для publishable/retained backup text/JSON/log-файлов применить единый helper на базе `redact_sensitive_url`: query parameter сохраняется, значение заменяется на `%5BREDACTED%5D`.
3. Не редактировать SQLite байтовой заменой. Для registry использовать штатную rebuild/SQL migration с backup и post-query verification.
4. Для каждого изменённого файла записать `before_sha256`, `after_sha256`, `redacted_count`, `reason`, `retention_class`.
5. Если credential мог быть действующим — передать владельцу на ротацию и записать `rotation_status`; redaction без rotation не закрывает security finding.

**Acceptance:**

- `unredacted_access_token_count=0` во всех publishable output, active registry и retained backups;
- `accessToken` остаётся только как redacted marker или в тестовом fixture, явно помеченном synthetic;
- output/enriched/normalized provenance и citations не изменены семантически;
- security report содержит до/после counts и SHA.

### Фаза 2. Canonical backup/restore

1. Зафиксировать canonical layout: `backup_root/manifest.json` и пути относительно project root (`output/...`, `artifacts/...`, `rag_storage/...`), без повторного `output/output` или `rag_storage/rag_storage`.
2. Расширить существующий backup/restore шаблон либо добавить отдельный validator, который:
   - обнаруживает legacy nesting;
   - строит явную mapping-таблицу source→restore target;
   - запрещает неоднозначные mappings;
   - проверяет SHA до и после dry-run restore.
3. Не переписывать старые audit artifacts молча: либо создать canonical sanitized copy, либо пометить legacy layout как `legacy_restore_only`.
4. Выполнить dry-run restore в изолированный каталог; production restore не запускать в этой фазе.

**Acceptance:** 100% записей manifest восстанавливаются в ожидаемые пути; SHA совпадают; nested ambiguity вызывает fail-closed ошибку; создан restore report.

### Фаза 3. Согласование manifest и документации

1. В remediation-ТЗ заменить фиксированное `expected=2` на `expected == len(manifest.sources)` и зафиксировать текущий final run `4/4`.
2. В README и operator runbook указать canonical manifest path, exact Windows provenance paths, активные DB paths и порядок backup→validate→load→post-check.
3. В `LUNA_GEMINI_CANARY_REPORT.md` явно разделить `historical evidence` и `current final status`; убрать двусмысленное «исправления выполняются» из текущего статуса.
4. В execution/pipeline plans добавить ссылки на final remediation reports и пометить незапущенную массовую обработку как historical decision, если она действительно не требуется.
5. Создать `accepted_risk_register_20260812.json` с полями:
   `id`, `finding`, `severity`, `owner`, `due_date`, `mitigation`, `status`, `evidence`, `expiry_or_review_date`.

Обязательные записи risk register: flattened Codex CLI roles, LightRAG alias warnings, legacy backup compatibility. Owner и due date не могут быть пустыми; если риск закрыт, статус `closed` и evidence обязательны.

**Acceptance:** docs не содержат противоречивых expected/count/status; risk register валиден по schema; все accepted risks имеют владельца и срок.

### Фаза 4. LightRAG warning disposition

1. Повторить read-only анализ финального load log и сгруппировать auto-merge warnings по entity/source/doc.
2. Для каждого warning выбрать ровно одно:
   - детерминированный canonical alias mapping с тестом и объяснением;
   - accepted warning в risk register с owner/deadline;
   - исправление входного entity label с отдельным backup и повторным isolated load.
3. Не считать warning failure, если документ `processed`, но не терять его из report.
4. Добавить regression test: warning не должен повышать `failed`, не должен оставлять `processing`, не должен менять counts без явного mapping.

**Acceptance:** warning report существует, у каждого предупреждения есть disposition; `processing=0`, `failed=0`, `inserted==expected`.

### Фаза 5. Release gate и повторное ревью

1. Выполнить штатные команды из `.venv`:

```powershell
Set-Location D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q --basetemp D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid\.pytest-closeout
.venv\Scripts\python.exe main.py validate enriched --fail-on-error
git diff --check
```

2. Проверить production invariants: 131 cards (или явно зафиксированный новый count), 0 invalid URLs, 0 `partial_segment_failure`, 0 semantic technical placeholders, FTS/registry counts согласованы, graph all processed, stale China/6/72 absent.
3. Повторить token scan по `output`, `rag_storage`, `artifacts` и backup manifests.
4. Проверить, что active configured SQLite paths — именно `artifacts/card_fts.sqlite` и `artifacts/source_registry.sqlite`; нулевые decoy-файлы не используются runtime.
5. Провести финальное независимое ревью diff + reports. Оно должно классифицировать все remaining warnings и не содержать новых P1/P2.

## 5. Финальный DoD

ТЗ считается закрытым только одновременно при выполнении всех пунктов:

1. Token scan: `unredacted=0` во всех publishable output, registry и retained backups; rotation status записан, если credential был потенциально действующим.
2. Canonical backup restore dry-run проходит SHA-валидацию без ambiguous nesting.
3. Manifest schema/документация согласованы; текущий финальный load отражает `expected=4`, `inserted=4`, либо динамическое `expected=len(sources)` и N/N.
4. LightRAG: `processed` у всех текущих документов, `processing=0`, `failed=0`, stale China/6/72 отсутствуют; все warnings имеют disposition.
5. Enriched validation: `131/131`, errors=0, warnings=0 (или новый count с baseline explanation).
6. `partial_segment_failure=0`; технические placeholders отсутствуют в semantic fields. Допустимый `[Instagram Reel: URL]` в source `text` не считать semantic placeholder.
7. Risk register заполнен; каждый accepted risk имеет owner, due date, mitigation и evidence.
8. Full pytest и Ruff green; diff check clean.
9. Финальный отчёт содержит branch/HEAD, changed files, counts, SHA manifests, token report, backup restore report, warning disposition, risk register и verdict.
10. Verdict: `READY_FOR_COMMIT`. `READY_FOR_PUSH` не выдавать автоматически; push — отдельное явное действие пользователя.

## 6. Обязательные артефакты

- `artifacts/closeout_baseline_20260812.json`;
- `artifacts/token_redaction_closeout_20260812.json`;
- `artifacts/backup_restore_closeout_20260812.json`;
- `artifacts/lightrag_warning_disposition_20260812.json`;
- `artifacts/accepted_risk_register_20260812.json`;
- `artifacts/accepted_manifest_load_report.json`;
- `artifacts/closeout_test_report_20260812.json`;
- `artifacts/closeout_final_review_20260812.md`.

Все отчёты должны быть machine-readable либо иметь machine-readable companion. При failed gate работа останавливается; частичный production result не принимается, rollback выполняется только из SHA-проверенного backup.
