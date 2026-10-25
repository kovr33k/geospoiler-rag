# Release evidence post-review

Дата: 2026-08-18  
Scope: незакоммиченный diff статического release-ТЗ относительно `HEAD`.  
Вердикт: **BLOCKED**, `READY_FOR_FINAL_RUN` отозван.

Production load, cold extraction, live smoke, media backfill, полный pytest,
новый release bundle и push в ходе ревью не выполнялись.

## Что проверено

- Полностью прочитаны изменённые producer, bundle builder, cold runner,
  smoke, manifest loader, verifier и regression-тесты.
- Прежние локальные проверки остаются зелёными: `114 passed`, `3 subtests`,
  Ruff, compileall и `git diff --check`.
- Выполнены отдельные read-only/temporary-directory reproductions для ключевых
  отрицательных сценариев.

## Findings

### P1-1. Exact report schema фактически не проверяется

`release_identity.stamp_report()` использует `setdefault`, а builder проверяет
только наличие части envelope. Поддельные `producer`,
`producer_schema_version`, `schema_version` и `source_inputs` с корректно
пересчитанным payload SHA принимаются. Воспроизведение: builder вернул
`status=passed` для `producer=forged-producer`, `wrong_schema_v999` и неверных
manifest/corpus SHA.

### P1-2. Builder может объявить повреждённый или неполный bundle успешным

Нет точного allowlist обязательных report kinds и запрета reserved kinds.
Передача пользовательского `run_index` дала `builder_status=passed`, после чего
verifier обнаружил `evidence_manifest_hash_mismatch:run_index.json`.
Одновременно verifier считает `run_index` необязательным и не сверяет его
schema, точный список kinds и canonical attempt path.

### P1-3. Cold per-source SHA являются копией manifest, а не измерением

`cold_graph_check._source_results()` переносит три SHA из manifest и verifier
сравнивает их обратно с тем же manifest. Воспроизведение вернуло все три
"валидных" SHA для физически отсутствующего normalized-файла. Cold storage не
имеет собственного deterministic file inventory, а загруженный `full_docs`
content не сравнивается с `graph_text`.

### P1-4. State inventory записывается, но не является release gate

Verifier не пересчитывает SHA всех manifest source/card/graph_text на момент
вердикта. `graph_processed` сравнивает только количество status rows, а свежий
exact set doc_id/source_id не строится. Provenance report не привязан к SHA
текущего RAG storage и не сравнивает source IDs с manifest. FTS/registry counts
попадают в JSON, но отсутствуют среди `checks`; URL coverage также не блокирует
release. Изменение ignored production data после producer reports может пройти
незамеченным.

### P1-5. Smoke retry history можно обойти

`--attempt-history` необязателен, исключение до сборки report вообще не
записывает попытку, а `_smoke_is_strict()` принимает singleton `attempts` с
только последней успешной попыткой. Reproduction вернул `True` для свежего
одноэлементного ledger. Smoke report хранит только counts, но не reference IDs,
URL и citation mapping, поэтому verifier не может независимо повторить
проверку citations.

### P1-6. Test evidence не является самодостаточным

Producer принимает exit codes и summary как внешние ручные аргументы, не
фиксирует фактические argv каждой команды и не хеширует summary input. Raw logs
остаются по абсолютным внешним путям и не копируются в bundle. После verifier
их можно удалить или изменить, а уже опубликованный verdict этого не покажет.

### P1-7. Error-path verifier сам падает

После исключения в `build_report()` fallback payload не содержит
`corpus.media.total`, но markdown renderer обращается к нему напрямую.
Воспроизведение с `build_report -> RuntimeError` завершилось
`KeyError: 'total'` вместо атомарно опубликованного FAILED report.

### P2-1. Несколько evidence producers всё ещё пишут неатомарно

Manifest-load report, portable-path report и основной smoke report используют
прямой `write_text`; smoke ledger использует предсказуемый общий `.tmp` без
fsync/lock. При остановке или параллельном запуске возможно усечение либо
перезапись evidence.

### P2-2. Поля lineage и command имеют двусмысленную семантику

Cold `inserted` всё ещё равен числу processed rows, а не возврату loader;
отдельные `inserted_by_loader/reused_processed` это частично исправляют, но
старое поле продолжает участвовать в gate. `command_argv` содержит один
склеенный command string, а не реальный argv.

### P2-3. Финальный verdict не самопроверяемый

`final_release_verification.json` публикуется атомарно, но не имеет собственного
payload SHA/малого verdict manifest. Naming `attempt-001`, `attempt-002` не
валидируется, поэтому снова возможны неоднозначные `final/final2` каталоги.

## Итог

P0: 0. P1: 7. P2: 3. Текущая реализация полезна как первый слой hardening, но
ещё не даёт права на финальный release run.
