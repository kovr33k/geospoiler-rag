# ТЗ: окончательное статическое закрытие release-evidence контура R3

Дата: 2026-08-18  
Статус: **READY FOR IMPLEMENTATION — FINAL RELEASE RUN FORBIDDEN**

## 1. Цель

Закрыть все дефекты, найденные повторным read-only ревью после реализации R2:

- 6 P1;
- 2 P2;
- 0 P0.

Это ТЗ исправляет только код, контракты, recovery-механику и regression-тесты.
В него **не входят** production manifest load, изменение `output/`, изменение
`rag_storage/`, cold extraction, live Late-Fusion smoke, полный release run,
создание нового passed bundle, commit, push, tag или deploy.

Финальный релизный прогон разрешается добавить в отдельное execution-ТЗ только
после повторного статического ревью с результатом **0 P0 / 0 P1 / 0 P2**.

## 2. Зафиксированные дефекты и их практический вред

### P1-1. Manifest допускает неоднозначный или расширенный JSON

`loader/ingest.py` читает manifest обычным `json.loads()`, а общий validator
не отклоняет неизвестные root, source-entry и `pipeline_identity` поля.
`scripts/cold_provenance.py` вызывает полный shared validator только для уже
полностью заполненного v2 identity.

Чем мешает: loader, cold proof и verifier могут по-разному понять один файл;
duplicate key или скрытое лишнее поле способно изменить смысл разрешённой
production-загрузки.

### P1-2. Test evidence можно сконструировать без запуска команд

Публичный `scripts.build_release_test_evidence.build_report()` принимает
готовые `summary_path`, logs и `exit_codes`; без runner receipts он создаёт
правдоподобные receipts самостоятельно.

Чем мешает: bundle может утверждать, что pytest/Ruff/compile/diff прошли,
хотя subprocess вообще не запускались.

### P1-3. Smoke не привязан к официальным десяти кейсам

Verifier требует десять уникальных ID, но не exact-набор `CASES` из
`scripts/late_fusion_activation_smoke.py` и не frozen digest вопроса/profile.

Чем мешает: сложные официальные запросы можно заменить более лёгкими и всё
равно получить `10/10`.

### P1-4. FTS, registry и metadata index сверяются не по полным строкам

Текущий verdict в основном сравнивает `source_id` sets и counts. Не доказано
точное соответствие `normalized_path`, `card_path`, `post_url`, content hash,
reference URL и всех разрешённых metadata aliases.

Чем мешает: правильный ID может ссылаться на неправильную карточку или URL, а
stale alias может оставаться в индексе незамеченным.

### P1-5. Media contract проверяется только в сторону missing

Verifier находит ожидаемые, но отсутствующие flags/issues, однако не отклоняет
лишние устаревшие pipeline-owned `quality_flags` и `extraction_issues`.

Чем мешает: успешно обработанное медиа может продолжать помечаться как
недоступное, и релиз ошибочно будет считаться корректным.

### P1-6. Late loader cleanup использует потенциальный basename

При финальной проверке статуса `loader/ingest.py` передаёт в cleanup
`status_doc.file_path`. LightRAG может вернуть basename вместо исходного
canonical path; тогда удаляется не тот metadata alias.

Чем мешает: неуспешный документ может остаться доступным через stale source
metadata после того, как loader уже сообщил об ошибке.

### P2-1. SQLite snapshot не является единым стабильным снимком

`scripts/graph_provenance_reconcile.py` отдельно хеширует DB-файлы и затем
читает таблицы несколькими соединениями; WAL/SHM и активный writer не
закрываются общим протоколом.

Чем мешает: SHA и строки отчёта могут относиться к разным моментам времени;
повторная проверка становится невоспроизводимой.

### P2-2. Аварийное завершение оставляет неполный audit trail или вечный lock

Test runner обрабатывает timeout, но не гарантирует failed receipt при
interrupt. Builder использует пустой `O_EXCL` lock; после выключения компьютера
stale lock блокирует следующий запуск без безопасной автоматической проверки.

Чем мешает: после poweroff теряется причина незавершённой проверки, а новый
релизный запуск может не стартовать до ручного удаления lock-файла.

## 3. Phase 0 — документация и разрешённые API

Перед изменением кода исполнитель обязан полностью прочитать:

- `loader/manifest_contract.py`;
- `loader/ingest.py`, функции `load_from_manifest()` и `load_texts()`;
- `loader/storage.py`, функции `_sync_source_metadata_index()` и
  `_remove_source_metadata_index()`;
- `scripts/release_identity.py`, strict JSON и atomic write helpers;
- `scripts/build_release_test_evidence.py`;
- `scripts/late_fusion_activation_smoke.py`;
- `scripts/build_release_verification.py`;
- `scripts/graph_provenance_reconcile.py`;
- `scripts/build_release_evidence.py`;
- затронутые release-evidence tests.

Разрешённые существующие API, которые нужно переиспользовать:

- `scripts.release_identity.strict_json_loads()` — duplicate-key-safe JSON;
- `loader.manifest_contract.load_manifest_file()` — единая загрузка manifest;
- `loader.manifest_contract.validate_manifest_payload()` — pure validation;
- `scripts.release_identity.atomic_write_bytes()` и `atomic_write_json()` —
  durable file publication;
- `loader.storage._canonical_source_path()`, `_source_doc_id()`,
  `_rag_file_path()` — canonical document identity;
- `loader.storage._remove_source_metadata_index()` — cleanup по исходному
  canonical source path;
- `scripts.late_fusion_activation_smoke.CASES` — единственный источник
  официального smoke contract.

Запрещено:

- создавать второй manifest parser или второй список smoke-кейсов;
- применять `json.loads()` к release manifest;
- доверять basename как canonical source identity;
- принимать caller-supplied exit code как доказательство subprocess;
- лечить нестабильный SQLite snapshot повторными чтениями без snapshot
  protocol;
- вручную удалять непонятный lock без проверки ownership/liveness.

## 4. Phase 1 — exact accepted-manifest contract

### Реализация

1. В `loader/manifest_contract.py` зафиксировать exact allowed sets:
   - root v1 и root v2;
   - `pipeline_identity` v1 и v2;
   - source entry v1 и v2.
2. Validator обязан отклонять unknown fields детерминированной ошибкой с
   полным field path.
3. `load_manifest_file()` остаётся единственным file loader и использует
   `strict_json_loads()`.
4. `loader.ingest.load_from_manifest()` убрать собственный `json.loads()` и
   собственную расходящуюся structural validation. Он должен получать
   результат `load_manifest_file()` и прекращать работу до любых LightRAG и
   report mutations при ошибках.
5. `scripts/cold_provenance.py` обязан вызывать shared validator для любого
   manifest, заявленного как `accepted_manifest_v2`, даже если identity
   неполный. Incomplete v2 всегда fail-closed.
6. Bundle builder и final verifier должны использовать тот же shared result,
   а не повторять упрощённые проверки.

### Regression tests

- duplicate root key;
- duplicate key внутри source entry;
- unknown root field;
- unknown `pipeline_identity` field;
- unknown source entry field;
- incomplete accepted_manifest_v2;
- одинаковый deterministic error set в loader/cold/verifier;
- ни один invalid manifest не вызывает LightRAG mutation и не публикует
  успешный load report.

### Acceptance

- один JSON имеет одну трактовку во всех consumers;
- exact schema проверяется до чтения corpus payloads и до mutation;
- legacy v1 допускается только существующим явным compatibility flag.

## 5. Phase 2 — runner-owned test evidence

### Реализация

1. Удалить публичный режим `build_report(summary_path, logs, exit_codes, ...)`,
   способный создавать release evidence из caller-supplied результатов.
2. Разделить:
   - private pure formatter, принимающий только внутренний typed execution
     result;
   - public `run_gate()`, который сам запускает subprocess и возвращает
     complete receipts;
   - fixture-only helper в tests, который не доступен production CLI/builder и
     не создаёт stamped release report.
3. Каждый check receipt обязан происходить только из реально завершённого
   `subprocess.CompletedProcess` или явного timeout/interruption result.
4. Summary вычислять из этих же receipts; запрещено читать независимый
   caller-authored summary как источник статуса.
5. `main()` требует `--execute`, явный `--base-ref`, новый пустой staging dir и
   не поддерживает manual exit codes.
6. Verifier по-прежнему проверяет exact argv, cwd, timestamps, timeout,
   exit-code, log bytes/SHA, base/merge-base/HEAD identity.

### Regression tests

- прямой вызов production report builder с fake logs/exit codes невозможен;
- fake summary `0 failed` не перекрывает failed subprocess;
- monkeypatched subprocess подтверждает exact четыре вызова;
- reordered, extra или missing command блокирует evidence;
- повторное использование staging dir блокируется без overwrite.

### Acceptance

- release test report нельзя получить без прохождения `run_gate()`;
- статус и summary всегда выводятся из одних runner receipts;
- тестовые fixtures не экспортируются как release-capable API.

## 6. Phase 3 — frozen official smoke contract

### Реализация

1. Вынести из `late_fusion_activation_smoke.py` canonical serializer
   официальных `CASES`: для каждого case фиксируются `case_id`, exact question
   и profile.
2. Определить `OFFICIAL_CASES_SHA256` из canonical JSON или проверять digest,
   вычисленный из единственного shared constant; verifier не должен принимать
   digest, объявленный самим проверяемым report, как expected value.
3. Producer сохраняет:
   - exact official case set/version;
   - official spec SHA;
   - result SHA отдельно от spec SHA.
4. `_smoke_is_strict()` сравнивает exact case IDs и spec digest с shared
   official contract, а не только count/uniqueness.
5. Case order либо фиксируется и проверяется, либо canonical sort явно
   закрепляется одинаково у producer/verifier.

### Regression tests

- IDs `0..9` отклоняются;
- один изменённый вопрос при прежнем ID отклоняется;
- один изменённый profile отклоняется;
- reordered cases следуют выбранному и документированному правилу;
- точный официальный набор проходит.

### Acceptance

- `10/10` всегда означает именно десять официальных запросов;
- report не может сам назначить себе trusted expected digest.

## 7. Phase 4 — exact FTS/registry/metadata reconciliation

### Реализация

1. Из validated manifest и enriched cards построить deterministic expected
   tables со всеми identity-bearing полями:
   - source: `source_id`, canonical post URL и тип;
   - normalized: `source_id`, exact normalized path/hash;
   - enriched: `source_id`, exact card path/hash;
   - FTS card: `source_id` и content/card identity поля, влияющие на поиск;
   - references: `source_id`, reference type, sanitized canonical URL;
   - metadata index: exact canonical path, virtual path и только явно
     разрешённые aliases.
2. Считать actual rows в deterministic order и сравнивать полные normalized
   tuples, а не только `row[0]` или counts.
3. Отдельно отклонять:
   - missing row;
   - extra/stale row;
   - duplicate row;
   - field mismatch;
   - alias, не выводимый из canonical source contract.
4. Counts оставить только диагностикой; они не участвуют как самостоятельное
   доказательство exactness.
5. Machine report должен сохранять bounded mismatch examples и полные counts,
   не раскрывая sensitive URL query values.

### Regression tests

- заменить `card_path` при том же source ID и count;
- заменить post URL;
- заменить content hash;
- добавить stale metadata alias;
- удалить reference;
- добавить лишний FTS row;
- exact fixture проходит.

### Acceptance

- `fts_registry_exact=true` возможно только при полном равенстве expected и
  actual tuples;
- stale alias или неправильный URL всегда блокируют verdict.

## 8. Phase 5 — симметричный media-contract gate

### Реализация

1. Определить явные pipeline-owned sets для media-related:
   - `quality_flags`;
   - `extraction_issues` или их canonical prefixes.
2. Для каждой sidecar/card пары вычислить expected flags/issues из текущего
   metadata.
3. Сравнивать в обе стороны:
   - `missing = expected - actual`;
   - `stale_extra = actual_owned - expected`.
4. Пользовательские/semantic flags, не принадлежащие media pipeline, не
   удалять и не считать ошибкой этого gate.
5. Report должен показывать missing и stale_extra раздельно.

### Regression tests

- failed transcript без flag/issue блокируется;
- success transcript со stale `transcript_unavailable` блокируется;
- success visual со stale `visual_missing` issue блокируется;
- unrelated semantic flag допускается;
- exact current state проходит.

### Acceptance

- карточка не может ни скрывать media failure, ни продолжать заявлять уже
  исправленный failure.

## 9. Phase 6 — корректный loader cleanup

### Реализация

1. В `load_texts()` сохранить immutable attempted canonical source path в
   `doc_info` до insert.
2. `status_doc.file_path` использовать только как диагностическое поле.
3. Все cleanup branches, включая late non-processed status, вызывают
   `_remove_source_metadata_index(doc_info["source_path"])`.
4. Cleanup обязан удалить canonical и virtual aliases, выводимые существующим
   storage helper, независимо от basename в LightRAG status.
5. Machine error report сохраняет оба значения:
   - attempted canonical path;
   - observed status file path.

### Regression tests

- status возвращает basename, поздний status failed: canonical/virtual aliases
  удалены;
- status возвращает другой path: удаляется только identity попытки, не
  произвольный чужой source;
- success path aliases сохраняются;
- частичный batch failure не оставляет metadata residue.

### Acceptance

- cleanup никогда не выбирает target по basename;
- после failed insert source resolution не находит stale row.

## 10. Phase 7 — единый стабильный SQLite snapshot

### Реализация

1. Ввести один documented helper для snapshot каждой SQLite DB.
2. До чтения проверить:
   - regular file и containment;
   - отсутствие активного writer по выбранному безопасному механизму;
   - состояние `-wal`, `-shm`, `-journal`.
3. Рекомендуемый путь: SQLite read-only connection + `PRAGMA
   integrity_check`, затем `Connection.backup()` во временный private файл;
   все rows и SHA брать только из этой backup-копии.
4. Все таблицы одной DB читать в одном consistent transaction/snapshot.
5. Temp snapshot удалить в `finally`; путь temp не включать в release identity.
6. Если стабильность доказать нельзя, producer завершается failed; повторные
   best-effort чтения запрещены.

### Regression tests

- rows и SHA относятся к одной backup copy;
- активная mutation/WAL simulation даёт fail-closed или согласованный backup;
- integrity failure блокирует report;
- WAL-only committed row не приводит к main-file-SHA/row mismatch;
- temp cleanup выполняется при exception.

### Acceptance

- один snapshot report всегда описывает один физически согласованный момент;
- verifier может повторить snapshot и сравнить его SHA/rows детерминированно.

## 11. Phase 8 — crash-safe attempts и lock recovery

### Реализация test runner

1. Создавать attempt lifecycle record до первого subprocess:
   `started -> running:<gate> -> passed|failed|interrupted`.
2. При `TimeoutExpired`, `KeyboardInterrupt`, `SystemExit` и другом
   контролируемом interruption атомарно сохранять:
   - failed/interrupted receipt текущего gate;
   - уже завершённые receipts;
   - partial stdout/stderr, если доступны;
   - ненулевой final status.
3. Исключение после записи audit trail можно пробросить; бесшумно превращать
   interrupt в success запрещено.

### Реализация builder lock

1. Lock содержит versioned JSON: PID, process start identity, hostname,
   run/attempt ID, created_at и target path.
2. При наличии lock:
   - если owner жив и identity совпадает — fail as active writer;
   - если owner мёртв или PID reused — выполнить deterministic stale recovery;
   - malformed lock не удалять вслепую: переместить в отдельный audit artifact
     и продолжить только после доказательства отсутствия активного writer.
3. Предпочесть OS advisory/exclusive handle на время работы; JSON служит
   audit/recovery metadata, а не единственным mutex.
4. Cleanup lock выполнять в `finally`, но poweroff recovery не должен требовать
   ручного удаления.
5. Два параллельных writer не могут одновременно получить ownership.

### Regression tests

- KeyboardInterrupt во втором gate оставляет первый receipt и interrupted
  второй;
- poweroff-style stale PID lock восстанавливается;
- live-owner lock блокирует второй writer;
- PID reuse не принимается как тот же owner;
- malformed lock сохраняется как failure evidence;
- race двух writers даёт ровно одного победителя.

### Acceptance

- после аварии следующий запуск либо безопасно продолжает новым attempt, либо
  выдаёт понятный active-owner blocker;
- причина незавершённого предыдущего запуска остаётся в audit trail.

## 12. Phase 9 — verification до повторного ревью

Запустить только статические проверки затронутого контура:

1. targeted pytest для всех новых и существующих manifest/test/smoke/state/
   loader/lock tests;
2. targeted Ruff для изменённых Python-файлов;
3. `py_compile` изменённых модулей;
4. `git diff --check HEAD --`;
5. negative reproduction каждого из восьми findings;
6. read-only повторное ревью текущего diff.

Запрещено в этой фазе:

- полный release runner;
- live provider calls;
- production/cold smoke;
- manifest load;
- запись в `output/`, `rag_storage/`, FTS/registry;
- новый passed final bundle;
- commit/push/tag/deploy.

## 13. Обязательная матрица regression coverage

| Finding | Обязательный негативный тест | Gate |
|---|---|---|
| P1-1 manifest | duplicate/unknown/incomplete v2 | shared validator rejects |
| P1-2 tests | fake logs + exit_codes без subprocess | report impossible |
| P1-3 smoke | неофициальные 10 IDs | strict smoke false |
| P1-4 state | wrong path/URL/hash при том же ID/count | exact gate false |
| P1-5 media | stale extra pipeline flag/issue | media gate false |
| P1-6 cleanup | failed status с basename | no metadata residue |
| P2-1 SQLite | WAL/concurrent mutation | stable or fail-closed |
| P2-2 crash | interrupt и stale lock | audit preserved/recovered |

## 14. Статический Definition of Done

ТЗ считается выполненным только когда одновременно выполнено всё:

- exact manifest schema едина для loader/cold/builder/verifier;
- test evidence нельзя создать из caller-supplied exit codes;
- smoke доказывает exact official 10-case contract;
- FTS/registry/references/metadata index сверяются полными tuples;
- media pipeline-owned flags/issues совпадают с sidecar в обе стороны;
- failed loader cleanup всегда использует attempted canonical source path;
- SQLite report построен из одного стабильного snapshot;
- interruption оставляет failed audit trail;
- stale lock восстанавливается безопасно без ручного удаления;
- все targeted positive/negative tests зелёные;
- Ruff, py_compile и diff-check зелёные;
- повторное независимое read-only ревью: **0 P0 / 0 P1 / 0 P2**.

После этого статус меняется только на `READY_FOR_FINAL_RUN`. Сам финальный
прогон, production mutation и публикация release evidence выполняются
отдельным ТЗ и не являются частью данного исправления.

