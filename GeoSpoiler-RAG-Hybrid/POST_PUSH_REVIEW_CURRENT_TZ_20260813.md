# ТЗ: закрытие findings текущего post-push review

Дата: 2026-08-13
Проект: `D:\\RAG-GeoSpoiler\\GeoSpoiler-RAG-Hybrid`
Основание: свежий read-only review текущего dirty checkout, `docs/POST_PUSH_REVIEW_FOLLOWUP_EXECUTION_20260813.md` и независимые проверки кода и production-корпуса.

## 0. Текущий verdict

`BLOCKED`. Кодовая часть и regression-gates зелёные, но production acceptance не закрыта.

Зафиксировано:

- normalized: 129 текстов и 129 sidecar-файлов;
- enriched: 131 карточка, 131 уникальный `source_id`;
- FTS: cards 131, YouTube segments 22, Wiki 160;
- LightRAG: 129/129 `processed`, `processing=0`, `failed=0`;
- stale China/6 и China/72: 0 попаданий;
- `partial_segment_failure`: 0, технические placeholder-маркеры в semantic-полях: 0;
- media: 95 скачанных объектов (72 image, 23 video);
- visual: success 6/95, без финального статуса 89;
- STT: transcribed 2/23, failed 21, без статуса 72;
- sidecar→card: 20/21 failed-STT и 83/89 visual non-success не отражены корректно в карточках; всего 103 item-level mismatch;
- security scan `artifacts/`: 0 файлов и 0 occurrences, но issuer-side revoke/rotate credential не подтверждён.

Нельзя объявлять `READY_FOR_COMMIT` или «media-контур закрыт», пока нижеописанные gates не пройдены.

## 1. P1 — выбрать и выполнить media acceptance

Рекомендуемый и основной режим — **wide acceptance 95/95**. Scoped Ultra-режим не используется как скрытый backdoor и может быть принят только отдельным письменным решением владельца с явным backlog для остальных 91 объектов.

1. До любых live-вызовов использовать уже подготовленный immutable backup:
   `artifacts/luna_full_reenrich/followup_media_backfill_20260813/`.
   Проверить SHA, preflight и отсутствие параллельных loader/backfill процессов.
2. Выполнить canary на разнородной выборке (минимум 10 объектов: image, short video, long video, STT-400, premature-response, unavailable media) с `TRANSCRIPTION_ENABLED=true` и текущей конфигурацией Luna/OpenRouter.
3. В canary report зафиксировать без токенов: `input_sha`, provider/model, route, attempts, HTTP status/error class, latency, artifact path, финальный status.
4. Проверить multipart STT-контракт OpenRouter: field name, MIME, model, размер/длительность, retry/fallback. Синтетически воспроизвести HTTP 400 и преждевременное завершение ответа. Caption-only не считать успешной media extraction.
5. После успешного canary запустить `main.py media-backfill --all` с checkpoint/resume и machine-readable report. При отказе отдельного объекта сохранять `failed|unavailable` sidecar, а не оставлять статус отсутствующим.
6. Для каждого media item выполнить deterministic join sidecar→card:
   - failed STT ⇒ `transcript_unavailable` + `extraction_issues` с media error;
   - visual не `success` ⇒ `visual_missing` issue либо явный quarantine/review status;
   - mismatch fingerprint/contract ⇒ штатная re-enrichment, не ручная правка JSON.
7. После backfill штатно re-enrich затронутые карточки с `media_contract_version=media-contract-v2`, затем атомарно rebuild normalized/enriched projections, FTS и source registry.
8. Сгенерировать accepted manifest только из текущих SHA/provenance; LightRAG загружать только через manifest с `expected == inserted == len(sources)` и terminal `processed` для каждого документа.

### Gate media

- 95/95 имеют финальный auditable status;
- нет silent caption-only fallback;
- 0 необъяснимых sidecar→card mismatches;
- технические flags/issues соответствуют sidecar;
- `partial_segment_failure=0`;
- citations, provenance, URL и language checks зелёные;
- graph/FTS/registry отражают именно обновлённые карточки.

## 2. P1 — credential safety

1. Не печатать значения секретов; зафиксировать только локальный SHA-инвентарь `.env` и список активных параметров.
2. Сохранить `RERANKER_ENABLED=false` и пустой `RERANKER_API_KEY`; удалить неиспользуемые legacy provider keys из runtime-конфигурации, если они не нужны текущему маршруту.
3. Владелец провайдера должен выполнить revoke/rotate всех credential, которые могли попасть в `.env`, logs, artifacts или quarantine.
4. Получить машинно проверяемое issuer-side evidence и обновить `RISK-TOKEN-ROTATION-001` на `resolved` с датой, owner и ссылкой на evidence. До этого release verdict остаётся `BLOCKED`.
5. Повторить secret scan для tracked checkout, publishable `output/`, active registry и `artifacts/`. Synthetic fixtures разрешены только по явному allowlist и не должны маскировать live-looking values.

## 3. P1/P2 — portable paths и sidecar integrity

1. Мигрировать все 95 исторических `file_path`/`native_media_paths` со старого абсолютного `C:\\WikiRag\\...\\media_cache\\...` на canonical relative `media_cache/...` (старое значение оставить только как non-authoritative legacy field при необходимости).
2. Выполнить SHA до/после и relocation test из другой checkout root.
3. Проверить atomic pair update для `.txt` и `.meta.json`, fault-injection при сбое между заменами, recovery из backup.
4. Любой malformed/missing `message_id` должен пропускаться fail-closed с записью в report, не останавливая весь backfill и не обновляя соседний media item.

## 4. P2 — backup/manifest/restore contract

1. Унифицировать schema текущего backup (`backup_manifest.json`) и `scripts/validate_backup_restore.py`: один формат полей (`path`, `bytes`, `sha256`, root), версия mapping и однозначный backup root.
2. Проверять именно актуальный 160-file backup, а не старый манифест, который закономерно даёт SHA mismatch после последующих изменений.
3. Сохранить fail-closed правила для traversal, symlink, duplicate physical file, duplicate canonical target и повторных `output/<namespace>/<channel>/<channel>`/`rag_storage/rag_storage`.
4. Legacy backups либо canonicalize с mapping report, либо маркировать `legacy_restore_only`; не считать их полноценным restore evidence.
5. Для SQLite выполнять `integrity_check`; в отчёте хранить counts, SHA before/after, mapping и восстановленные файлы.

## 5. P2 — dynamic audit и документация

1. Убрать оставшиеся historical hard-coded проверки `131/135` из `scripts/luna_reenrich_audit.py`; использовать текущие динамические инварианты 129/127+2 и ровно 4 YouTube source/video IDs.
2. Один canonical status report должен содержать branch/HEAD, dirty inventory, flags, scope, counts, manifest path, report paths, следующий review date и verdict `BLOCKED|READY_FOR_COMMIT`.
3. Historical `LUNA_GEMINI_*` документы явно маркировать `historical`; current status не должен обещать выполненные live gates.
4. Обновлять baseline только после всех code/data/doc изменений; не использовать старый baseline как доказательство текущего closeout.

## 6. P2 — release hygiene

1. Оставить `.pytest-*` и все scratch basetemp вне checkout; перед staging выполнить full-repo token scan.
2. Разделить tracked source/tests, publishable reports и ignored raw quarantine. В commit добавлять только согласованный evidence allowlist; raw secrets/quarantine не публиковать.
3. После завершения data gates выполнить один reviewable staging/commit без `git reset --hard` и без смешивания unrelated changes.
4. Повторить:

```powershell
Set-Location D:\\RAG-GeoSpoiler\\GeoSpoiler-RAG-Hybrid
.venv\\Scripts\\ruff.exe check .
.venv\\Scripts\\python.exe -m pytest -q --basetemp D:\\GeoSpoiler-test-tmp\\final
.venv\\Scripts\\python.exe main.py validate enriched --fail-on-error
.venv\\Scripts\\python.exe scripts\\security_artifact_redaction.py scan --root artifacts
.venv\\Scripts\\python.exe scripts\\validate_backup_restore.py --manifest <current-manifest> --report <report>
git diff --check
```

## 7. Final DoD

Работа считается закрытой только если одновременно выполнены:

1. `RISK-TOKEN-ROTATION-001=resolved` с внешним evidence;
2. wide media acceptance 95/95 и deterministic sidecar→card join;
3. 0 silent fallback, 0 необъяснимых media mismatch, 0 `partial_segment_failure`;
4. manifest `expected == inserted == len(sources)`, `processing=0`, `failed=0`;
5. FTS/registry/LightRAG согласованы, stale China/6 и China/72 отсутствуют;
6. portable paths и backup SHA/integrity gates зелёные;
7. token scan publishable scope = 0;
8. dynamic docs без stale `2/2`, `131/135` и ложного current wording;
9. full pytest, Ruff, enriched validator и diff check зелёные после последнего baseline;
10. final review report содержит evidence и verdict `READY_FOR_COMMIT`.

До выполнения live media canary/backfill и внешней credential rotation текущий verdict остаётся `BLOCKED`.
