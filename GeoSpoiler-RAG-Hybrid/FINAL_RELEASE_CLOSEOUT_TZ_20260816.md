# ТЗ: окончательное закрытие замечаний и выпуск GeoSpoiler-RAG

Статус документа: `APPROVED_FOR_EXECUTION`, исполнение ещё не начато.

Этот документ заменяет прежние post-push/follow-up ТЗ как единственный актуальный
release-контракт. Исторические отчёты сохраняются только как evidence и не
используются для доказательства текущей готовности.

## 1. Цель

Получить один воспроизводимый release-кандидат, в котором:

- закрыты все открытые P1 и технически исправимые P2 последнего ревью;
- текст/enrichment выполняются через Luna;
- STT остаётся на Gemini/OpenRouter;
- `LATE_FUSION_ENABLED=true`, `RERANKER_ENABLED=false`;
- generic RAG остаётся Enriched-first;
- все graph-документы загружены в новый LightRAG через fail-closed manifest;
- для graph-корпуса выполнена полноценная KG-экстракция, а не payload projection;
- corpus, FTS, source registry, provenance и citations согласованы;
- секреты отсутствуют в release tree и retained artifacts;
- working tree сведён к одному проверенному commit;
- итоговый статус — `READY_FOR_RELEASE`.

Push, создание Git-тега и публикация релиза выполняются только после успешного
DoD и отдельной финальной команды пользователя.

## 2. Неподвижные границы

Не менять без отдельного решения:

- Luna — основной text/enrichment runtime;
- Gemini/OpenRouter — media transcription;
- Qwen embedding contract и его размерность;
- Late-Fusion — включён;
- reranker — выключен;
- Wiki не включается в этот релиз;
- русскоязычные semantic-поля Enriched;
- YouTube FTS и timestamp/start URL contract;
- удалённые `Китай/6` и `Китай/72` не восстанавливаются;
- старые human A/B оценки не повторяются и новые оценки от пользователя не
  запрашиваются;
- никакой загрузки в live graph до полного preflight и проверенного backup.

## 3. Исходный baseline

На момент составления ТЗ:

- branch: `work/rag-v2-snapshot-20260810`;
- HEAD: `2c3d749`;
- normalized: 129;
- Enriched: 131 (`127` generic + `4` YouTube episode cards);
- media: 95 (`72` image + `23` video);
- visual: `success=95`;
- STT: `transcribed=21`, `no_speech=2`, `failed=0`;
- generic media/card join: `127/127`, mismatches `0`;
- LightRAG: 129 processed documents — 127 generic и только 2 из 4 YouTube
  episode cards;
- в graph отсутствуют episode sources
  `telegram:3215620297:27:youtube:vVmfBZvpfHg` и
  `telegram:3889026624:2:youtube:wXyMfwS5ZYs`;
- FTS: cards 131, YouTube segments 22, Wiki 160;
- registry: sources 133, normalized 129, enriched 131, references 32/32 URL;
- tests: 674 passed, 1 skipped, 42 subtests;
- текущий graph был обновлён через payload projection, а entity/relation topology
  сохранена из baseline;
- по явному решению пользователя `RISK-TOKEN-ROTATION-001` считается выполненным
  и при исполнении должен быть переведён в `closed`;
- working tree dirty и не является release-артефактом.

Перед исполнением эти числа перечитать заново. Любой drift записать в baseline,
а не подгонять данные под числа этого документа.

## 4. Обязательные существующие API

Использовать существующие точки входа, не обходить их прямым редактированием
LightRAG JSON/GraphML/SQLite:

- `loader.ingest.load_from_manifest(rag, manifest_path)` — fail-closed load;
- `scripts/graph_provenance_reconcile.py --report ...` — provenance gate;
- `python main.py validate enriched --fail-on-error` — card validation;
- `python main.py registry rebuild` — source registry;
- штатные builders Card FTS и YouTube segment FTS;
- `scripts/validate_backup_restore.py --manifest ... --report ...`;
- `scripts/security_release_scan.py --report ...`;
- `scripts/security_artifact_redaction.py scan --root artifacts`;
- `scripts/luna_reenrich_control.py prepare|activate|restore`;
- `python main.py media-backfill ...` — dry-run по умолчанию, live только с
  `--execute-live`;
- официальные методы LightRAG для insert/delete/finalize.

Запрещено:

- напрямую править `kv_store_*.json`, GraphML и vector stores;
- считать `processed` по отсутствующему doc status;
- сопоставлять источники по basename;
- загружать ad-hoc список без accepted manifest;
- писать секреты или полные credential-like URLs в отчёты;
- перезаписывать release evidence повторным запуском с тем же run ID;
- запускать два LightRAG writer одновременно;
- принимать payload projection за новую KG-экстракцию.

## 5. Фаза 0 — freeze, baseline и recovery point

### Реализация

1. Убедиться, что нет активных Python/Codex/LightRAG writer-процессов.
2. Создать уникальный `release_run_id` вида
   `release_YYYYMMDDTHHMMSSZ_<git-short-sha>`.
3. Создать внешний backup до любых изменений:
   - `output/normalized`;
   - `output/normalized_youtube`;
   - `output/enriched`;
   - `output/youtube_segments`;
   - `rag_storage`;
   - FTS/registry SQLite и их sidecars;
   - `state/enrichment_progress.json`;
   - конфигурационную identity без secret values.
4. Сформировать `backup_restore_closeout_v1` с canonical relative paths,
   `bytes`, SHA-256 и source identity.
5. Проверить backup через `scripts/validate_backup_restore.py`.
6. Записать baseline JSON: branch, HEAD, dirty files, config identity, corpus
   counts, graph counts, FTS/registry counts, media matrix, security counts.

### Проверка

- backup manifest readable;
- `declared == checked`, SHA mismatch `0`, nested/collision/traversal `0`;
- backup находится вне активных production directories;
- rollback-команда проверена в dry-run;
- baseline отчёт не содержит секретов.

### Stop condition

При любом backup/hash/path error дальнейшее исполнение запрещено.

## 6. Фаза 1 — закрытие code blockers

### 6.1. Единый live-execution guard

Старый `transcribe backfill` привести к тому же контракту, что
`media-backfill`:

- default — dry-run;
- live разрешён только с `--execute-live`;
- `--dry-run` и `--execute-live` взаимоисключающие;
- прямой вызов handler без флага также не выполняет внешний запрос;
- в machine report сохраняются `execute_live`, selector, attempted и routes.

Тесты:

- parser default не вызывает STT;
- `transcribe backfill --limit 1` не вызывает provider;
- только `--execute-live` разрешает вызов;
- конфликт флагов завершается nonzero до egress.

### 6.2. Visual cache и artifact safety

Исправить:

- cache identity обязана проверять `frame_count` как integer в диапазоне
  `1..MEDIA_VISION_MAX_FRAMES`;
- `read_bytes`, stat и hash errors преобразуются в
  `VisualExtractionResult(status="failed")`;
- visual artifact записывается через temp + fsync + atomic replace;
- failed/unavailable результаты сохраняют model, SHA, prompt version, error,
  route/attempts и latency;
- повтор failed/corrupt artifact не принимается за cache hit;
- абсолютный artifact path не является canonical metadata.

Тесты:

- missing/zero/negative/overflow `frame_count` -> cache miss;
- permission/race read failure -> failed result, pipeline не падает;
- fault injection до replace сохраняет старый валидный artifact;
- malformed JSON -> cache miss и повторный bounded analysis;
- success/failure/unavailable sidecar persistence.

### 6.3. Fail-closed media identity

- parent и item `message_id` должны быть положительными integer;
- malformed ID пропускается с machine-readable reason;
- `file_path` разрешается только внутри approved media roots;
- traversal, symlink escape и foreign absolute path отклоняются до чтения и
  до передачи в LLM/provider.

### 6.4. Backup restore transaction

Доработать `luna_reenrich_control.restore`:

- валидировать и `record.path`, и `record.source` относительно approved roots;
- проверить все source files/SHA/SQLite integrity до первого move;
- создать restore journal со статусами `planned`, `moved`, `copied`, `verified`;
- при сбое откатить все уже изменённые targets, а не только текущий;
- journal закрывается `committed` только после post-restore verification;
- повторный запуск умеет обнаружить незавершённый journal и только восстановить
  прежнее состояние.

### 6.5. Run/evidence identity

Убрать hardcoded `RUN_ID` из `scripts/luna_reenrich_audit.py`:

- `--run-id` или `--run-dir` обязателен для записи;
- read-only audit может вывести JSON в stdout;
- output directory не должна существовать до запуска;
- report содержит git commit/tree hash, config identity, corpus manifest SHA,
  model identity, timestamp и command;
- повторный запуск с тем же ID fail-closed, без overwrite.

### Фазовый gate

- targeted tests для CLI/media/restore/audit — зелёные;
- Ruff на изменённых файлах — зелёный;
- `git diff --check` — зелёный;
- ни один test не выполняет реальный egress без явного live marker.

## 7. Фаза 2 — переносимые metadata и corpus reconciliation

### Реализация

Добавить детерминированный migration CLI с режимами `scan`, `apply`, `verify`.
Он должен:

- заменить authoritative и derived absolute paths на canonical project-relative
  paths;
- обработать `file_path`, `native_media_paths`, `image_paths`,
  `visual_artifact_path`, `transcript_path`, visual/transcription artifact
  references, registry card paths и LightRAG metadata aliases;
- удалить зависимости от `C:\WikiRag` и абсолютного текущего `D:` checkout;
- проверять containment и существование целевого файла;
- не менять semantic content;
- записать before/after SHA и поле-level changes;
- обновлять JSON atomically;
- обновлять SQLite transactionally с `integrity_check`;
- не применять raw byte replacement к SQLite.

После миграции:

1. Пересобрать normalized/card fingerprints.
2. Синхронизировать `media-contract-v2`.
3. Пересобрать только deterministic path/provenance/graph/search projections;
   LLM semantic-поля не перегенерировать, если их input/evidence SHA не менялся.
4. Если semantic SHA неожиданно изменился — остановить фазу и восстановить
   affected card из backup.
5. Пересобрать Card FTS, YouTube FTS и source registry штатными builders.

### Quality flag policy

Блокирующие:

- `partial_segment_failure`;
- `extraction_unstable`;
- media failure без соответствующего pipeline-owned flag/issue;
- технический placeholder внутри semantic fields;
- sidecar/card fingerprint mismatch.

Допустимые только при валидном evidence:

- `mixed_topics`;
- `timestamps_unavailable`;
- `unclear_source_chain`;
- `very_short_text`;
- `mostly_boilerplate`.

`contains_legacy_media_placeholders` удалить после проверки, что placeholder
остался только в `ignored_blocks`. Для YouTube исключение не оставлять: semantic
fields чистые — flag снимается; semantic fields грязные — карточка
перегенерируется или quarantined.

### Acceptance

- старых `C:\WikiRag` paths: `0`;
- absolute checkout paths в publishable output/registry/RAG metadata: `0`;
- 129 normalized и 131 Enriched parseable;
- generic join 127/127, mismatches 0;
- media 95/95 имеют terminal visual status;
- video/audio 23/23 имеют terminal STT/no-speech status;
- blocking quality flags: 0;
- semantic technical markers: 0;
- FTS/registry counts соответствуют динамическому corpus accounting.

## 8. Фаза 3 — security closeout и закрытие credential-риска

### Local remediation

1. Расширить release scanner до полного release tree:
   - tracked files;
   - untracked files, которые будут staged;
   - `output`, publishable `artifacts`, release docs;
   - исключить `.git`, `.venv`, pytest scratch и внешний quarantine.
2. Не выводить найденное secret value; report содержит только path, rule ID,
   count и redacted digest.
3. Восстановить truthful audit trail для quarantine 20260816:
   - отдельный immutable manifest для фактически перемещённых файлов;
   - before/quarantine SHA;
   - sanitized replacement SHA;
   - reason и retention decision.
4. No-op повторный scan не перезаписывает предыдущий remediation report.

### User-authorized completion

По явному решению пользователя старый `accessToken` считается отозванным или
ротированным для целей этого release-контракта. Дополнительная внешняя проверка
issuer не является блокирующим gate этого ТЗ.

При исполнении:

- записать в risk register `RISK-TOKEN-ROTATION-001.status=closed`;
- в release report указать `completion_basis=user_attestation`;
- не записывать значение токена, полный URL или provider secret;
- сохранить только дату, run ID и redacted fingerprint, если он уже существует.

### Acceptance

- release tree findings: 0;
- retained artifacts findings: 0;
- quarantine manifest matches files N/N;
- `RISK-TOKEN-ROTATION-001.status=closed` с `completion_basis=user_attestation`;
- полный credential value нигде не попадает в Git/evidence/logs.

## 9. Фаза 4 — строгий release manifest и новый LightRAG

### 9.1. Release graph contract

Создать manifest schema `accepted_manifest_v2`, совместимую с v1 и явно
описывающую все 131 Enriched graph sources:

- `127` generic `document_kind=enriched_card` — canonical Enriched
  `graph_text`, provenance path под `output/normalized`;
- `4` YouTube episode `document_kind=enriched_card` — canonical Enriched
  `graph_text`, provenance path под `output/normalized_youtube`;
- каждый entry содержит accepted, source_id, canonical source path, document
  path, optional card path, SHA-256 исходника, SHA-256 graph payload и pipeline
  identity;
- approved normalized roots задаются по subtype generic/YouTube;
- duplicate source_id/path/payload collision, missing file, SHA mismatch,
  traversal или symlink escape блокируют весь preflight;
- `expected == len(manifest.sources)` вычисляется динамически.

В v1 поведение не менять. Добавить regression tests для обеих schema.

### 9.2. Изолированная полная сборка

1. Создать пустой candidate `rag_storage` вне live path.
2. Инициализировать LightRAG с той же embedding/model identity.
3. Загрузить manifest v2 только через `load_from_manifest`.
4. Выполнять одного writer последовательно; не запускать конкурирующие loader.
5. Дождаться terminal status каждого source.
6. Для всех 131 выполнить штатную KG extraction; не копировать baseline
   entities/relations и не использовать payload projection.
7. Сохранить отдельный machine report:
   - expected;
   - inserted;
   - skipped;
   - failed;
   - processed;
   - source IDs;
   - model/config/manifest identity;
   - elapsed и per-document errors без prompt/secret payload.

Длительность не является основанием для fallback. Loader можно возобновлять
только по immutable manifest identity и terminal statuses.

### 9.3. Candidate verification

- expected=131, inserted=131, skipped=0, failed=0;
- doc status processed=131, processing=0, failed=0;
- provenance reconciliation matched=131, missing=0, ambiguous=0;
- China/6 и China/72 отсутствуют;
- fake/legacy source IDs отсутствуют;
- 127 generic payload SHA совпадают с card `graph_text`;
- 4 YouTube episode payload SHA совпадают с card `graph_text`;
- entity/relation stores созданы candidate-run, а не скопированы из baseline;
- retrieval возвращает только разрешённые canonical sources.

### 9.4. Activation

Только после зелёного candidate verification:

1. Остановить writers.
2. Повторно проверить candidate manifest/SHA.
3. Атомарно переключить live `rag_storage` через maintenance transaction.
4. Старый graph сохранить как rollback archive.
5. После activation повторить status/provenance/query checks.
6. При любой ошибке автоматически вернуть прежний graph целиком.

## 10. Фаза 5 — runtime acceptance без нового human A/B

Не просить пользователя повторно оценивать старые 10 A/B-кейсов.

Выполнить автоматический release smoke на фиксированных 10 разнородных запросах:

- `LATE_FUSION_ENABLED=true`;
- `RERANKER_ENABLED=false`;
- pipeline во всех кейсах `late_fusion`;
- legacy fallback 0/10;
- каждая citation разрешается в source registry;
- cited source имеет canonical HTTP(S) URL;
- unknown/missing citations = 0;
- Wiki evidence = 0;
- latency и token usage записаны;
- ответы не содержат retrieval-only fields как evidence;
- corpus/config/manifest identity зафиксированы.

Это smoke/citation gate, не blind A/B и не требует пользовательских рейтингов.

Минимальный provider smoke:

- Luna smoke — только штатный `scripts/luna_smoke.py --confirm-live`;
- media provider smoke выполняется только на маленьких уже разрешённых fixtures,
  без повторного массового canary и без изменения production cards;
- yt-dlp вызывается через project `.venv`; для release установить
  `YTDLP_REQUIRE_CURRENT=true` и принять только `status=ok`.

## 11. Фаза 6 — полная verification matrix

Запускать из project `.venv`, pytest basetemp — вне репозитория.

Обязательные проверки:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp D:\GeoSpoiler-release-<run_id>
.\.venv\Scripts\ruff.exe check .
git diff --check
.\.venv\Scripts\python.exe main.py validate enriched --fail-on-error
.\.venv\Scripts\python.exe scripts\graph_provenance_reconcile.py --report <run_dir>\graph_provenance.json
.\.venv\Scripts\python.exe scripts\security_release_scan.py --report <run_dir>\security_release.json
.\.venv\Scripts\python.exe scripts\security_artifact_redaction.py scan --root artifacts
.\.venv\Scripts\python.exe scripts\validate_backup_restore.py --manifest <backup_manifest> --report <run_dir>\backup_restore.json
```

Дополнительно проверить оба feature profiles там, где они влияют на query:

- Late-Fusion on / reranker off — release profile;
- Late-Fusion off / reranker off — rollback profile;
- reranker on не является release gate.

Ни один warning не игнорировать молча. Для ожидаемого warning требуется rule,
owner, reason и disposition в report.

## 12. Фаза 7 — immutable release evidence

Создать новый каталог `artifacts/release_<run_id>` без переиспользования старых
путей. Он содержит только sanitized publishable evidence:

- `baseline.json`;
- `config_identity.json` без secrets;
- `backup_manifest.json`;
- `backup_restore_report.json`;
- `path_migration_report.json`;
- `security_release_report.json`;
- `quarantine_manifest.json` без credential value;
- `accepted_manifest_v2.json`;
- `accepted_manifest_load_report.json`;
- `graph_provenance_report.json`;
- `corpus_acceptance.json`;
- `media_card_join_report.json`;
- `runtime_smoke_report.json`;
- `test_report.json`;
- `risk_register.json`;
- `FINAL_RELEASE_REPORT.md`.

Каждый report содержит `release_run_id`, timestamp, git tree/commit, manifest
SHA и schema version. Evidence append-only; повторный запуск получает новый ID.

`FINAL_RELEASE_REPORT.md` обязан прямо различать:

- выполненную полноценную KG extraction;
- runtime smoke;
- automated tests;
- внешнее credential evidence;
- accepted non-P1 residual risks.

Нельзя переносить старые слова `READY`, `accepted` или старые counts без
повторной проверки текущего release identity.

## 13. Фаза 8 — clean commit и выпуск

### Commit preparation

1. Вывести explicit allowlist файлов для staging.
2. Не добавлять raw backups, quarantine, `.env`, SQLite runtime, logs, caches,
   pytest scratch и secret-bearing evidence.
3. Добавить только source, tests, docs и sanitized release manifests/reports.
4. Повторить security scan уже по staged tree.
5. Проверить, что untracked release-critical files отсутствуют.
6. Создать один release commit с фактическим scope.
7. На commit повторить short verification: Ruff, targeted tests, security scan,
   manifest/provenance hashes.

### Push/release

После статуса `READY_FOR_RELEASE` и отдельной команды пользователя:

- push текущей release branch без force;
- создать release tag только с явно согласованной версией;
- сохранить commit SHA и remote confirmation в финальном сообщении;
- не выполнять merge в master без отдельного указания.

## 14. Финальный Definition of Done

Релиз разрешён только если одновременно выполнено всё:

1. Full test suite green; Ruff и diff-check green.
2. 129 normalized / 131 Enriched валидны по динамическому accounting.
3. Media/card mismatches 0; blocking quality flags 0.
4. Publishable absolute/stale paths 0.
5. FTS/registry counts согласованы; references URL N/N.
6. Новый isolated LightRAG собран полноценной KG extraction.
7. Manifest load `expected=131`, `inserted=131`, `skipped=0`, `failed=0`.
8. Live graph `processed=131`, provenance `131/131`.
9. China/6, China/72 и fake paths отсутствуют.
10. Late-Fusion runtime smoke 10/10, fallback 0, invalid citations 0.
11. Security findings 0 во всём staged/release tree и retained artifacts.
12. Token rotation/revocation закрыта по user attestation; P1 risk closed.
13. Backup/restore report green; rollback archive существует.
14. Все release evidence имеют один run/config/corpus/commit identity.
15. Нет hardcoded historical run IDs в текущих audit scripts/docs.
16. Нет release-critical untracked файлов.
17. Working tree clean после release commit.
18. Остаточные P2 допустимы только как явно accepted constraints с owner,
    mitigation и expiry; открытых неисправленных P1/P2 нет.
19. Финальный verdict ровно `READY_FOR_RELEASE`.

Если хотя бы один пункт не выполнен, verdict — `BLOCKED` с machine-readable
причиной. Запрещено заменять непрошедший gate ручным текстом «по сути готово».

## 15. Порядок исполнения

Фазы выполняются строго последовательно:

`0 backup -> 1 code blockers -> 2 path/data reconciliation -> 3 security and rotation -> 4 fresh graph -> 5 runtime smoke -> 6 verification -> 7 evidence -> 8 commit/release`.

После каждой фазы сохраняется отдельный report и проверяется rollback. Нельзя
переходить к следующей фазе при красном gate предыдущей.
