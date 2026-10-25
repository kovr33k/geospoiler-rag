# ТЗ R23: атомарная связь scan/seal/commit и корректное SQLite recovery после committed-uncertain

Статус: **EXECUTED — INDEPENDENT REVIEW REQUIRED; RELEASE EXECUTION BLOCKED**

Дата независимого ревью: 2026-08-27.

## 1. Контекст и границы

R22 проверен напрямую в основном потоке, без субагентов. Проверялись только
изменённые release publication, artifact/security scan и SQLite remediation
контракты. Cold/live/final release execution, публикация настоящего bundle и
полный corpus/all-card run не запускались.

Не менять RAG-архитектуру, retrieval, enrichment, модельные профили, corpus,
manifest contents и feature flags.

До исполнения этого ТЗ release gate остаётся закрытым. Зелёные R22 regressions не
разрешают release execution, потому что новые детерминированные race/fault
reproductions проходят через текущие guards.

## 2. Результат ревью

Подтверждено: **P0=0, P1=3, P2=1**.

### P1-1. `PreparedPublicationTree.commit()` не связывает sealed tree с атомарным commit

`scripts/release_identity.py:198-235` сначала повторно вычисляет inventory, затем
отдельно выполняет rename. Между завершением inventory и rename содержимое source
tree можно изменить.

Windows удерживает handle только корневого каталога. Этот handle запрещает rename
самого root, но не запрещает создание/замену дочерних entries. Детерминированный
fault injection добавил `attacker.txt` внутри `_windows_rename_open_directory()`:

```text
committed=true
attacker_published=true
```

POSIX дополнительно проверяет identity lexical source entry в
`scripts/release_identity.py:221-227`, но затем
`_posix_rename_noreplace()` переименовывает строковое имя. Подмена source после
`os.stat(..., dir_fd=...)` и до `renameat2()` публикует другой directory object:

```text
committed=true
attacker_published=true
validated_stranded=true
```

Следствие: утверждение R22 «опубликован тот же object/tree, который прошёл scan и
seal» не выполняется ни на Windows, ни на POSIX.

### P1-2. `scan_release_artifact_tree()` допускает ABA-подмену между inventory и analysis

`scripts/release_identity.py:1687-1688` отдельно читает initial inventory,
`scripts/release_identity.py:1704-1789` повторно читает файлы для verdict, а
`scripts/release_identity.py:1790-1797` ещё раз читает final inventory.

Если исходный secret-bearing файл временно заменить clean-файлом только на время
analysis, а затем вернуть исходный inode, initial и final inventory совпадают.
Verdict при этом построен из clean bytes. Подтверждённый результат:

```text
calls=3
status=passed
finding_files=0
secret_restored=true
```

Bundle и verifier используют этот scan как разрешающий publication gate, поэтому
последующий `publication.seal()` закрепляет уже возвращённый secret-bearing tree и
не обнаруживает обход.

### P1-3. SQLite post-commit durability error восстанавливает старые WAL/SHM рядом с новым main

`scripts/security_artifact_redaction.py:670-671` устанавливает `published=True`
только после возврата `atomic_write_bytes_in_directory()`. Этот primitive может
успешно заменить main, а затем выбросить `ReleasePublicationCommittedError` при
fsync/cleanup.

В таком случае `published` остаётся `False`, и finally-блок
`scripts/security_artifact_redaction.py:678-685` восстанавливает retired WAL/SHM,
не сверяя hash уже опубликованного main с transition marker.

Детерминированный commit-then-error reproduction получил:

```text
exception=ReleasePublicationCommittedError
members=history.sqlite, history.sqlite-wal, history.sqlite-shm
integrity_check=ok
logical row снова содержит REAL_SECRET_VALUE
```

То есть после ошибки retained family может снова логически содержать secret, хотя
main уже заменён redacted snapshot. Это нарушает crash-aware family transition и
fail-closed remediation.

### P2-1. R22 regression suite не покрывает обязательные race/fault boundaries

`tests/test_release_evidence_r22.py` проверяет primitive напрямую, но не содержит
реальных swap-тестов для bundle, canonical verifier и failure artifact. POSIX test
останавливается на identity mismatch до rename и не инжектирует подмену между
identity check и `renameat2()`.

Artifact scan покрывает изменение content, но не ABA restore и не полный
add/remove набор. SQLite failure test падает на первом вызове
`atomic_write_bytes_in_directory()`, то есть до retirement/main publication; нет
commit-then-error теста main и fault matrix для каждого transition step. Static
guard проверяет наличие строк `prepared_publication_tree`/`publication.seal`, но
не запрещает опасные последовательности и path-based rename через AST/allowlist.

Из-за этих пробелов текущие `13 passed` не доказывают Definition of Done R22.

## 3. Требуемая реализация

### 3.1 Один immutable snapshot для artifact verdict и publication seal

Перестроить `scan_release_artifact_tree()` по single-read модели:

1. Под закреплённым root один раз перечислить scope.
2. Для каждого entry создать snapshot record: lexical relative name, node type,
   no-follow physical identity и, для regular file, bytes/hash из одного открытого
   descriptor/handle.
3. Из этих же records вычислять inventory digest, metadata findings, content
   findings и verdict. Запрещено повторно читать path для analysis.
4. Выполнить final freshness snapshot и сравнить scope, identities и hashes с
   analyzed records. Любая замена/add/remove/change даёт sanitized stable error.
5. Возвращать `inventory_sha256` analyzed snapshot в report.
6. `PreparedPublicationTree.seal()` должен принимать ожидаемый digest успешного
   preflight/scan и подтверждать его, а не создавать независимую разрешающую
   snapshot после scan.

Self-preflight manifest/hash reads в bundle/verifier также перевести на snapshots
или stable descriptor reads, связанные с тем же analyzed inventory.

### 3.2 Закрыть окно между final inventory и directory rename

Нельзя считать отдельную последовательность `inventory -> stat -> rename`
атомарным доказательством.

Минимальный обязательный контракт:

- Windows: удерживать до завершения commit handles всех sealed regular files без
  `FILE_SHARE_WRITE | FILE_SHARE_DELETE`, а directory handles без delete sharing;
  запретить появление новых entries посредством закреплённого private staging
  container; после rename сверить target root identity и полный inventory с sealed
  snapshot до возврата success.
- POSIX: source должен находиться в private staging container с проверенными
  owner/mode и pinned parent FD; удерживать FDs всех sealed entries, повторно
  сверять namespace непосредственно до и сразу после `renameat2(RENAME_NOREPLACE)`;
  target root identity после rename обязан совпасть с pinned source FD.
- При post-rename mismatch не возвращать обычный success. Canonical target
  quarantine/recovery и receipt должны отражать `committed_durability_uncertain`
  либо более точный `committed_identity_mismatch`; pass receipt не создаётся.
- Явно задокументировать, что canonical directory нельзя потреблять до успешного
  post-commit identity/inventory receipt.
- Если платформа не позволяет обеспечить заявленный threat model, fail closed.
  Не оставлять path-based fallback и не заявлять более сильную гарантию, чем даёт
  primitive.

### 3.3 SQLite transition должен восстанавливаться по фактическому commit state

Свести process-level exception recovery и next-run crash recovery к одному helper:

1. Transition marker записывается durably до первого retirement.
2. После любого исключения, включая `ReleasePublicationCommittedError`, helper
   читает marker и текущий main через pinned identity.
3. Если current main hash равен `redacted_sha256`, старые retired sidecars удаляются,
   исходные sidecars не восстанавливаются, marker удаляется после durable cleanup.
4. Если main hash не равен redacted hash, все уже retired members возвращаются в
   исходные имена; mixed family запрещена.
5. Если state нельзя однозначно классифицировать, ничего не угадывать: сохранить
   marker/quarantine, вернуть stable recovery-required outcome и блокировать
   дальнейшую remediation/publication.
6. Исходный `ReleasePublicationCommittedError` не подавлять. Report/CLI обязан
   показывать `committed=true`, recovery action и итоговый family state.
7. Cleanup staging, retired members и marker должен иметь directory fsync и быть
   идемпотентным при повторном запуске.

### 3.4 Усилить static и runtime contracts

- Заменить строковый static test AST-проверкой registered directory producers:
  запрещены прямые `os.replace`, `Path.replace`, `durable_replace` для directory
  publication вне общего primitive.
- Проверять порядок `snapshot/preflight -> seal(expected_digest) -> commit ->
  postcommit verify`, а не только наличие отдельных вызовов.
- Не использовать monkeypatch `os.name` для доказательства платформенной
  безопасности production branch. Инжектировать нижележащие helpers.

## 4. Обязательные regression tests

1. Windows: добавить child после final inventory, но до handle rename; publication
   не возвращает success, attacker entry не становится подтверждённым canonical.
2. POSIX: заменить source name после entry identity check, но до `renameat2()`;
   wrong directory не получает подтверждённый canonical outcome, validated source
   не считается опубликованным.
3. Post-rename target identity mismatch на Windows и POSIX даёт committed-uncertain
   identity outcome и не создаёт pass receipt.
4. Bundle integration: ABA swap clean/secret во время artifact analysis не даёт
   `passed` и не публикует подтверждённый bundle.
5. Те же integration regressions для canonical final verifier и обеих
   failure-publication веток.
6. `scan_release_artifact_tree()`: secret -> clean -> тот же secret inode между
   initial/analysis/final даёт failed; analyzed snapshot digest относится к bytes,
   из которых построены findings.
7. Artifact scan add, remove, replace и metadata-only directory mutation дают
   freshness failure с sanitized paths.
8. SQLite: main replacement commit succeeds, parent fsync throws; recovery удаляет
   old WAL/SHM, retained logical DB остаётся redacted, outcome committed-uncertain.
9. SQLite fault matrix: marker write, retirement каждого WAL/SHM/journal member,
   main pre-commit failure, main post-commit error, retired cleanup, marker cleanup.
10. После каждого fault либо полностью восстановлена original family, либо полностью
    завершена redacted family; mixed state и потеря recoverable quarantine запрещены.
11. Повторный recovery после каждого промежуточного state идемпотентен.
12. AST static guard обнаруживает прямой path-based directory rename и неверный
    порядок scan/seal/commit в каждом registered producer.
13. Старые R20-R22 fault-injection tests продолжают проверять committed outcome и
    не ослабляются.

## 5. Порядок проверок

1. Новые R23 deterministic race/fault tests.
2. R20-R23 publication/security/remediation matrix.
3. Release identity, evidence, verification, remediation и security scan contracts.
4. Ruff по изменённым Python-файлам, compileall, `git diff --check`.
5. Один общий unit/regression suite только после зелёных targeted/static checks.
6. Новый независимый прямой read-only review с `0 P0 / 0 P1 / 0 P2`.

Полный corpus/all-card run, cold/live smoke и final release execution не входят в
R23. Они остаются последней проверкой всего цикла и запускаются только если после
исправлений и чистого независимого review всё ещё нужны.

## 6. Definition of Done

- verdict, inventory digest и seal происходят из одних immutable snapshot records;
- ни mutation child tree после seal, ни POSIX source-name swap не могут получить
  подтверждённый canonical success;
- target после rename доказуемо совпадает с pinned source object и sealed inventory;
- SQLite committed-uncertain recovery никогда не соединяет новый main со старыми
  sidecars;
- любая fault point оставляет либо recoverable original, либо целостную redacted
  family с честным outcome;
- новые adversarial regressions сначала воспроизводят текущие дефекты, затем
  проходят после исправления;
- targeted/static/common suites зелёные;
- отдельный независимый review подтверждает `0 P0 / 0 P1 / 0 P2`.

## 7. Evidence текущего ревью

```text
R20-R22 + security_release_scan matrix: 47 passed, 2 warnings
git diff --check по R22 production/tests: passed (существующее CRLF/LF warning)
Windows post-seal child mutation:       confirmed, wrong content published
POSIX identity-check/rename swap:       confirmed, wrong object published
Artifact scan ABA restore:              confirmed, status=passed with secret present
SQLite main commit-then-error:           confirmed, old WAL restores secret row
release gate:                            BLOCKED — DO NOT RUN
```

## 8. Фактическое исполнение 2026-08-29

Работа восстановлена после оборванного контекста по текущему worktree и
`tests/test_release_evidence_r23.py`; сохранённого gstack checkpoint не было.
Исполнение продолжено напрямую, без субагентов.

Реализовано:

- artifact verdict, inventory digest и publication seal связаны с одним immutable
  snapshot; bundle/final/failure self-preflight выполняется на materialized copy
  тех же snapshot bytes;
- `PreparedPublicationTree` проверяет source identity и sealed inventory после
  rename; mismatch не остаётся под canonical именем, а durably переносится в
  уникальный quarantine с честным committed-uncertain outcome;
- artifact ABA, scope add/remove, Windows post-seal mutation и POSIX source-name
  swap закрыты детерминированными regressions;
- SQLite transition recovery принимает решение по фактическому hash main,
  сохраняет hashes реально retiring WAL/SHM/journal family и не соединяет новый
  main со старыми sidecars;
- marker/retired cleanup переведён на общий durable unlink: POSIX unlink + parent
  fsync, Windows write-through rename из активного имени перед удалением tombstone;
- committed SQLite outcome передаёт `recovery_action` в CLI/report;
- static regression использует AST для запрета direct `os.replace` в registered
  directory producers и проверяет binding snapshot к `seal(expected_digest)`.

Фактические проверки:

```text
R23 targeted:                          11 passed
R19-R23 + identity/contracts/security: 100 passed, 2 skipped, 2 warnings
full unit/regression suite:             1141 passed, 4 skipped,
                                        2 warnings, 42 subtests passed
latest post-review cleanup fixes:       100 passed, 2 skipped, 2 warnings
ruff:                                   passed
compileall:                             passed
git diff --check:                       passed (existing CRLF/LF warnings only)
```

Общий suite был выполнен до последних локальных cleanup/quarantine уточнений.
После них повторно прошла вся затронутая 100-test матрица; общий suite повторно не
запускался.

Cold/live smoke, настоящий release publication, final release execution,
commit/push/deploy и полный corpus/all-card run не выполнялись.

Следующий шаг: отдельный независимый read-only review. До результата
`0 P0 / 0 P1 / 0 P2` release gate остаётся **BLOCKED — DO NOT RUN**.
