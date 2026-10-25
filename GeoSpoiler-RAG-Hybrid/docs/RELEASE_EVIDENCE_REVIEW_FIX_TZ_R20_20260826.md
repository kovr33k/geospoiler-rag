# ТЗ R20: race-free quarantine и единая durable publication state machine

Дата: 2026-08-26
Статус: **ИСПОЛНЕНО — RELEASE READINESS НЕ ОБЪЯВЛЕНА**

## 1. Основание

После исполнения R19 выполнен новый прямой review текущего worktree без
субагентов. Cold/live/final release workflow и полный corpus/all-card run не
запускались.

## 2. Подтверждённые проблемы

### P1-1. Quarantine parent можно подменить после validation

`security_artifact_redaction.apply_remediation()` проверяет destination перед
`_copy_to_quarantine()`, но не удерживает проверенные directory objects. Между
проверкой и `os.open()` обычный parent можно заменить junction.

Детерминированный Windows reproduction:

```text
rejected_after_copy=true
outside_copy=true
source_unchanged=true
passed_report_exists=false
```

Поздняя повторная проверка предотвращает redaction source и публикацию passed
report, но внешний write уже состоялся.

### P1-2. Headless subprocess contract покрывает не все release producers

После R19 `stdin=DEVNULL` есть в test runner, но отсутствует как минимум в:

- `scripts/build_release_verification.py::_git_value`;
- `scripts/security_release_scan.py::_git_file_names`;
- `scripts/luna_reenrich_control.py::_git`;
- live smoke subprocess в `scripts/luna_reenrich_control.py::prepare`.

На Windows invalid inherited stdin может вызвать `DuplicateHandle` до запуска
child process.

### P1-3. Windows atomic writer не подтверждает durability

`release_identity.atomic_write_bytes()` после file fsync вызывает обычный
`os.replace()`, а для `os.name == "nt"` полностью пропускает directory durability.
CPython реализует Windows replace через `MoveFileExW(MOVEFILE_REPLACE_EXISTING)`;
для синхронной записи move metadata Win32 предоставляет отдельный
`MOVEFILE_WRITE_THROUGH`.

Текущий тест маскирует пробел, подменяя `os.name` на `posix`, то есть не проверяет
реальный Windows publication path.

### P1-4. Directory publishers обходят commit outcome

Bundle и final-verifier публикуют каталоги собственными `os.replace()` и локальными
fsync-блоками. Bundle подавляет parent fsync error. Final verifier после
post-commit exception попадает в обычный failure path и при существующем final
directory может напечатать payload с `status=passed`, несмотря на exit code 1.

## 3. Цель

1. Исключить запись за quarantine/report boundary при подмене любого parent.
2. Сделать каждый subprocess официальных release producers независимым от stdin.
3. Ввести один durable replace primitive для файлов и каталогов с честным commit
   outcome на POSIX и Windows.
4. Не смешивать `committed_durability_uncertain` с `not_committed` и никогда не
   печатать `passed` после publication error.

## 4. Реализация

### 4.1 Destination locking

- На Windows открыть quarantine/report root и parent chain через `CreateFileW`
  с `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT` и без
  `FILE_SHARE_DELETE`.
- Повторно проверить каждый component после получения handles.
- Удерживать handles от последней проверки до copy/hash/report write.
- Existing quarantine target удерживать без delete-sharing; report-файлы
  заменять по лексическому final name внутри уже заблокированной parent chain,
  чтобы собственная atomic replace не конфликтовала с target handle.
- На остальных платформах сохранить fail-closed no-follow validation и добавить
  отдельный regression contract.

### 4.2 Headless subprocess inventory

- Добавить `stdin=subprocess.DEVNULL` во все перечисленные calls.
- Добавить AST regression: каждый `subprocess.run()` во всех registered release
  producer modules либо явно использует DEVNULL, либо документирован как
  интерактивный (в текущем registry интерактивных calls нет).

### 4.3 Durable replace primitive

- В `release_identity` добавить общий `durable_replace(source, target)`.
- POSIX: `os.replace`, затем fsync parent; ошибка после replace даёт
  `ReleasePublicationCommittedError(committed=True)`.
- Windows: `MoveFileExW(REPLACE_EXISTING | WRITE_THROUGH)`; Win32 failure до
  успешного возврата считается not-committed.
- `atomic_write_bytes`, bundle publisher и final-verifier directory publisher
  должны использовать только этот primitive.

### 4.4 Honest finalization

- Bundle при committed-uncertain создаёт отдельный sanitized sibling receipt и
  сохраняет committed exception type.
- Final verifier при committed-uncertain не удаляет committed output, не запускает
  обычный retry/failure publication и печатает только
  `status=committed_durability_uncertain`, `committed=true`, exit code 1.
- Failure directories также публикуются через общий primitive.

## 5. Regression tests

1. Actual Windows junction swap exactly before quarantine copy: outside file не
   создаётся, source не меняется, passed report отсутствует.
2. Swap report directory before each report write: outside report не создаётся.
3. Registry-wide AST/headless subprocess test.
4. Windows `MoveFileExW` flags включают `REPLACE_EXISTING | WRITE_THROUGH` для
   existing target и только `WRITE_THROUGH` допустим для new directory.
5. POSIX pre/post-commit fault matrix сохраняет `not_committed`/`committed`.
6. Bundle/final verifier fault injection проверяет отдельный uncertain outcome и
   отсутствие ложного `passed`.
7. Повторная запись существующего report остаётся атомарной, а target, появившийся
   после проверки отсутствия quarantine-файла, не переопределяется как допустимый
   idempotent result.
8. `cold_graph_check` делегирует публикацию shared release writer и не подавляет
   post-commit durability failure.

## 6. Проверки

1. R20 отдельно.
2. R18-R20 security/publication/headless matrix.
3. R10-R20 release/security suite.
4. R3/R12 subprocess tests в обычном pipe.
5. Ruff, compileall, `git diff --check`.
6. Общий unit/regression suite только после зелёных targeted/static checks.
7. Финальный прямой read-only review без P0/P1/P2.

Полный corpus/all-card run, cold/live и final release execution не входят в R20 и
остаются отложенными до конца всего цикла проверок.

## 7. Definition of Done

- race reproduction не создаёт ни байта вне destination root;
- все official release subprocess calls headless-safe;
- Windows publication использует write-through primitive;
- file и directory publication имеют одинаковую commit semantics;
- committed-uncertain никогда не отображается как passed/not-committed;
- targeted/static/common suites зелёные;
- финальный review не содержит P0/P1/P2.

## 8. Результат исполнения

Исполнение завершено 2026-08-26 в основном потоке, без субагентов.

Реализовано:

- Windows destination root и вся parent chain удерживаются без delete-sharing до
  завершения quarantine/report write;
- existing quarantine target фиксируется отдельным handle, а ранее отсутствующий
  target принимается только через exclusive create;
- atomic report writer сохраняет лексический final component и безопасно заменяет
  существующий report внутри заблокированного parent;
- все subprocess calls зарегистрированных release producers используют
  `stdin=DEVNULL`;
- общий `durable_replace` использует `MoveFileExW` с write-through на Windows и
  parent fsync с честным committed outcome на POSIX;
- file writer, bundle, final verifier, failure directories и cold-graph report
  используют единую publication state machine;
- committed-but-durability-uncertain сохраняется отдельным outcome/receipt и не
  отображается как `passed`.

Проверки:

```text
R20:                         12 passed
R10-R20 + security/backup:  250 passed, 3 skipped
R3/R7-R9 + contracts:       143 passed
common suite:              1105 passed, 4 skipped, 42 subtests passed
ruff / compileall / diff --check: passed
```

Четыре общих skip и три skip в release-наборе относятся к уже существующим
platform/privilege-условиям; настоящие Windows junction/locking regressions R20
выполнены без skip. Финальный прямой read-only review R20 scope не выявил
P0/P1/P2.

Cold/live/final release workflow и полный corpus/all-card run не запускались.
