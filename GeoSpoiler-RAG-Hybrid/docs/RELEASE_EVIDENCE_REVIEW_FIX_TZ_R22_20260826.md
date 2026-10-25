# ТЗ R22: exact prepared-tree publication и целостная SQLite/WAL remediation

Статус: **EXECUTED — INDEPENDENT REVIEW REQUIRED; RELEASE EXECUTION BLOCKED**

Дата ревью: 2026-08-26.

## 1. Контекст и границы

R21 закрыл подмену publication parent непосредственно внутри rename, но новый
прямой review подтвердил четыре оставшихся дефекта на границах publication и
security remediation.

ТЗ выполняется без cold/live/final release execution и без полного прогона
корпуса/всех карточек. Полный corpus/all-card run остаётся последней проверкой
всего цикла: только после реализации R22, targeted/static/unit regression и
следующего независимого review с `0 P0 / 0 P1 / 0 P2`, если прогон всё ещё нужен
для подтверждения последних изменений.

Не менять RAG-архитектуру, retrieval, enrichment, модельные профили, corpus,
manifest contents и feature flags.

## 2. Подтверждённые дефекты

### P1. Проверенное temporary tree можно заменить до захвата source handle

`build_release_evidence`, canonical final verifier и failure artifact сначала
выполняют self-preflight/secret scan по строковому path и только затем вызывают
`durable_replace()`. Source directory handle открывается уже внутри rename.

Воспроизведение:

1. создать temp tree с `validated.txt`;
2. выполнить `durable_prepare_tree()` и `scan_release_artifact_tree()` — status
   `passed`;
3. перед `durable_replace()` переименовать проверенное дерево и создать под тем же
   именем каталог с `attacker.txt`;
4. текущий код успешно публикует `attacker.txt`.

Следствие: canonical artifact не обязан быть тем деревом, для которого прошли
self-preflight, hash checks и secret scan.

### P1. Security scan не подтверждает identity/freshness после анализа

`security_release_scan.scan()` строит snapshot bytes и вычисляет из них digest и
verdict, но после анализа не проверяет, что candidate inventory и physical file
identities всё ещё соответствуют snapshot.

Подтверждённый тест заменяет clean candidate на файл с token сразу после анализа
snapshot bytes. Report возвращает `status=passed`, `finding_files=0`, тогда как
текущий lexical path уже содержит secret.

### P1. SQLite remediation игнорирует WAL/SHM как часть логической БД

`apply_remediation()` читает и карантинирует только bytes основного `.sqlite`, а
`_redact_sqlite_snapshot()` создаёт snapshot также только из main-file bytes.
Committed строки, находящиеся в `-wal`, не входят в эту копию.

Подтверждённый WAL fixture дал следующие результаты:

- scan нашёл secret в логической `history.sqlite`;
- main-only snapshot не содержал даже созданной таблицы;
- apply завершился `redaction verification failed`;
- quarantine copy не содержала логическое содержимое исходной БД;
- рядом остались скрытые `snapshot-wal`/`snapshot-shm`;
- исходная логическая БД по-прежнему содержала secret.

Контракт «byte-for-byte original в quarantine до замены retained copy» для SQLite
в WAL-режиме не выполняется.

### P2. Secret metadata пустого каталога не проваливает artifact-tree scan

`scan_release_artifact_tree()` увеличивает `metadata_occurrences` для каждого
entry, но создаёт finding только после проверки `path.is_file()`. Поэтому пустой
каталог с secret-bearing именем возвращает одновременно
`status=passed` и `metadata_occurrences=1`.

## 3. Требуемая реализация

### 3.1 Prepared-tree publication должна удерживать source identity до preflight

Добавить в `scripts/release_identity.py` один общий publication primitive/context
для directory tree, например `prepared_publication_tree(source, target)`.

Обязательный порядок:

1. проверить sibling relationship и закрепить target parent;
2. открыть source final directory no-follow/reparse-aware;
3. сохранить source identity и удерживать тот же handle/FD;
4. через закреплённый source выполнять durability preparation, self-preflight,
   inventory/hash checks и secret scan;
5. непосредственно перед commit повторно подтвердить соответствие lexical source
   тому же открытому object;
6. переименовать именно удерживаемый source object и сохранить честный
   `committed_durability_uncertain` outcome при post-commit ошибке.

Windows:

- source directory открыть через `CreateFileW` без `FILE_SHARE_DELETE` до первого
  preflight read;
- preflight не должен повторно переходить через незакреплённый source root;
- commit выполнить тем же handle через `SetFileInformationByHandle`;
- existing canonical target не заменять: publication обязана fail closed.

POSIX:

- source и parent открыть с `O_DIRECTORY | O_NOFOLLOW`;
- preflight выполнять через descriptor alias/`dir_fd`;
- перед descriptor-relative rename сравнить `fstat(source_fd)` с entry identity;
- при недоступных безопасных descriptor semantics завершаться ошибкой, без
  path-based fallback.

Перевести на primitive:

- `scripts/build_release_evidence.py`;
- canonical и failure publication в `scripts/build_release_verification.py`;
- все другие registered directory producers, если они используют тот же разрыв
  prepare/check/rename.

Не оставлять production-ветки, существующие только для monkeypatch тестов
`os.name`/`platform.system()`; platform branches тестировать через нижележащие
helpers.

### 3.2 Security scan: snapshot verdict плюс final freshness gate

Расширить snapshot record стабильной physical identity:

- Windows: volume/file identity из открытого handle;
- POSIX: минимум `st_dev`, `st_ino`, type, size и `mtime_ns` из `fstat`;
- path metadata и content digest должны относиться к одному открытому candidate.

После анализа выполнить final inventory/freshness pass:

- повторно перечислить тот же scope;
- сравнить набор physical identities, lexical names и hashes с analyzed snapshots;
- любое появление, исчезновение, замена или изменение candidate даёт
  `status=failed` и стабильный sanitized error code;
- report digest и finding counts остаются производными от analyzed snapshot bytes,
  а freshness mismatch отражается отдельной ошибкой;
- root identity удерживается на протяжении scan либо проверяется до и после.

Тот же контракт применить к `scan_release_artifact_tree()` там, где его результат
разрешает publication.

### 3.3 SQLite main/WAL/SHM обрабатывать как одну source family

Для finding типа SQLite определить семейство:

- main database;
- `-wal`, если существует;
- `-shm`, если существует;
- иные SQLite journal sidecars, которые реально участвуют в выбранном режиме.

Требования:

1. Закрепить parent и identity всех существующих членов family до initial hash.
2. Получить согласованный logical snapshot через SQLite backup/locking semantics,
   учитывающие committed WAL pages; копирование только main bytes запрещено.
3. До изменения retained copy сохранить в quarantine либо точные bytes всей
   исходной family с per-file hashes, либо документированный эквивалентный backup,
   из которого доказуемо восстанавливается исходная логическая БД. Текущий
   контракт byte-for-byte означает предпочтительно первый вариант.
4. Redaction выполнять в приватном staging directory; все его `-wal`, `-shm` и
   journal sidecars входят в cleanup как на success, так и на failure.
5. Перед публикацией проверить `PRAGMA integrity_check`, отсутствие secrets и
   сохранность всех нецелевых строк/таблиц.
6. Публикация main/sidecar transition должна быть crash-aware и fail closed.
   Нельзя оставлять новый main со старыми sidecars или наоборот.
7. При любой ошибке report обязан честно показать failure; quarantine остаётся
   пригодным для восстановления, а приватные snapshot sidecars не остаются рядом с
   source.

Не запускать remediation против live writer без доказанного exclusive SQLite lock.

### 3.4 Metadata findings для каталогов и иных entries

В `scan_release_artifact_tree()` обрабатывать secret-bearing metadata независимо
от `is_file()`:

- directory/other entry с sensitive path создаёт sanitized finding или отдельную
  metadata finding запись;
- `occurrences` включает все metadata occurrences;
- инвариант: `metadata_occurrences > 0` всегда означает `status=failed`;
- raw secret-bearing path не попадает в report/error text.

## 4. Обязательные regression tests

1. Bundle: заменить полностью проверенный temp root перед commit — publication
   rejected, canonical output отсутствует, attacker tree не публикуется.
2. Тот же тест для canonical final verifier и failure artifact.
3. Windows: source handle захвачен до self-preflight и тот же handle используется
   при rename.
4. POSIX: source FD identity mismatch перед rename блокирует publication.
5. Existing canonical target, включая пустой каталог, никогда не заменяется.
6. Security scan: clean snapshot заменяется secret-файлом после analysis — report
   failed с freshness error.
7. Security scan: candidate добавлен/удалён после snapshot — report failed.
8. Artifact-tree scan: аналогичные replace/add/remove races блокируют publication.
9. SQLite WAL fixture, где schema и secret row находятся только в WAL: scan видит
   secret, quarantine восстанавливает исходную logical DB, retained DB сохраняет
   schema/нецелевые rows и содержит только redacted value.
10. SQLite failure injection на каждом шаге main/WAL/SHM transition: нет смешанного
    family state и нет потерянного recoverable original.
11. После success и failure отсутствуют private `snapshot-wal`, `snapshot-shm` и
    journal debris.
12. Empty secret-named directory даёт failed artifact scan, ненулевые
    `occurrences`/`metadata_occurrences` и sanitized finding path.
13. Static guard запрещает возврат к path-based rename после pinned preflight.

## 5. Порядок проверок

1. Новые R22 targeted race/WAL/metadata tests.
2. R19-R22 publication/security/remediation matrix.
3. Release identity, evidence, verification, remediation и security scan contracts.
4. Ruff по изменённым Python-файлам, compileall, `git diff --check`.
5. Один общий unit/regression suite только после зелёных targeted/static checks.
6. Новый независимый прямой read-only review.

Не выполнять в рамках R22 полный corpus/all-card run, cold/live smoke, final release
execution, publication настоящего release bundle, commit, push или deploy.

## 6. Definition of Done

- опубликованное directory tree является тем же source object, который прошёл
  durability, self-preflight, hash/inventory checks и secret scan;
- security report не может вернуть `passed`, если scope изменился до завершения
  scan;
- SQLite WAL/SHM family сохраняется, редактируется и восстанавливается целостно;
- failure cleanup не оставляет snapshot sidecars;
- любая sensitive path metadata, включая пустые каталоги, проваливает scan;
- все R22 и затронутые regression/static checks зелёные;
- следующий независимый review подтверждает `0 P0 / 0 P1 / 0 P2` до любого
  release execution.

## 7. Фактическое исполнение 2026-08-27

R22 исполнено напрямую без субагентов.

Реализовано:

- `PreparedPublicationTree` закрепляет source до preflight, требует sealed tree
  inventory и выполняет no-replace commit тем же Windows handle либо через POSIX
  source/parent descriptors;
- bundle, canonical final verifier и обе failure-publication ветки переведены на
  prepared-tree primitive;
- file reads сравнивают path identity, opened descriptor identity и post-read
  identity;
- security scan и artifact-tree scan выполняют final inventory freshness gate;
- metadata пустых каталогов и иных entries формирует finding и проваливает scan;
- SQLite main/WAL/SHM/journal сохраняются в quarantine как family, logical snapshot
  создаётся через SQLite `serialize()` под `BEGIN IMMEDIATE`, retained DB проходит
  redaction и integrity verification;
- для SQLite transition добавлен same-directory recovery journal: после hard crash
  следующий запуск либо восстанавливает retired sidecars старого main, либо
  завершает cleanup, если hash canonical main уже соответствует redacted snapshot;
- приватный SQLite staging и retired sidecars очищаются/восстанавливаются на
  success и process-level failure;
- старые R20/R21 fault-injection tests переведены с удалённой точки
  `durable_replace` на `PreparedPublicationTree.commit` без ослабления проверок;
- добавлен `tests/test_release_evidence_r22.py` с Windows, POSIX, freshness,
  metadata, WAL, rollback и crash-recovery regressions.

Фактические проверки:

- R22 targeted suite: `13 passed`;
- финальная затронутая R20-R22/security matrix после review-исправлений:
  `61 passed`;
- расширенная release/security/backup matrix: `98 passed, 3 skipped`;
- один общий unit/regression suite после первоначальных targeted/static gates:
  `1127 passed, 4 skipped, 2 warnings, 42 subtests passed`;
- последние локальные review-исправления повторно закрыты затронутой 61-test
  матрицей, без повторного общего suite;
- Ruff: passed;
- compileall: passed;
- `git diff --check`: passed.

Прямой self-review в основном потоке дополнительно нашёл и исправил:

- ложный reject POSIX `/proc/self/fd` directory alias;
- повторное pinning-конфликтующее чтение внутри Windows prepared tree;
- позднюю установку committed state после rename;
- утечку source handle/FD при ошибке до входа в cleanup boundary;
- отсутствие автоматического hard-crash recovery для SQLite family transition.

Из-за прямого запрета на субагентов независимый review в рамках исполнения не
проводился. Release gate остаётся закрытым до следующего отдельного review с
`0 P0 / 0 P1 / 0 P2`.

Полный corpus/all-card run, cold/live smoke и final release execution не
запускались и остаются отложенными до самого конца всего цикла проверок.
