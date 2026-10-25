# ТЗ R19: destination boundaries, evidence freshness и Windows subprocess contract

Дата: 2026-08-25
Статус: **ИСПОЛНЕНО — RELEASE READINESS НЕ ОБЪЯВЛЕНА**

## 1. Основание

После исполнения R18 проведён новый read-only review изменённого release-evidence
scope в основном потоке, без субагентов. Production-код не изменялся. Cold/live,
final release workflow и полный corpus/all-card run не запускались.

Текущая узкая матрица остаётся зелёной:

```text
30 passed, 1 skipped
```

При этом отдельные synthetic reproductions подтвердили пять не покрытых ею
дефектов.

## 2. Подтверждённые проблемы

### P1-1. CLI заранее resolve-ит source root и тем самым обходит R18 boundary

`scripts/security_artifact_redaction.py:463` вызывает
`safe_resolve_release_path(args.root)`, а затем передаёт уже resolved target в
`scan()`. Проверка `_validated_artifact_root()` больше не видит, что исходный CLI
argument был junction/symlink/reparse point.

Windows junction reproduction:

```text
raw_is_junction True
resolved_equals_target True
outside_findings [('artifact.txt', 1)]
```

То есть CLI читает файл за исходной lexical boundary, хотя direct unit tests
`scan(root)` зелёные.

### P1-2. Quarantine destination допускает запись через junction за пределы root

`scripts/security_artifact_redaction.py:394-395` строит destination из
`quarantine_root / finding.path` и передаёт его в `_copy_to_quarantine()` без
проверки компонентов destination. Pre-existing junction в quarantine переносит
запись во внешний каталог.

Подтверждённый Windows reproduction:

```text
status passed
outside_copy_created True
quarantine_nested_is_junction True
```

Remediation при этом завершает работу со `status=passed`.

### P1-3. Backup и portable reports не связаны с фактически проверенным snapshot

Backup payload в `scripts/validate_backup_restore.py:181` хранит только path к
source manifest. В нём нет hash прочитанных manifest bytes или identity
проверенного набора backup files. `source_inputs` на `:224` содержит hash accepted
release manifest, а не backup source manifest.

Portable report аналогично содержит counts, но не deterministic inventory digest.
После изменения candidate file старый отчёт остаётся `passed`, хотя новый scan уже
возвращает failure state:

```text
old_status passed
old_has_inventory_sha False
current_status changes_pending
current_unresolved 1
```

Final verifier на `scripts/build_release_verification.py:1576-1585` доверяет status
и counts этих отчётов и не пересчитывает backup/portable snapshot. Stamped report
поэтому можно replay-ить после изменения реально проверенных входов.

В том же boundary `scripts/validate_backup_restore.py:88` использует обычный
`json.loads`, поэтому duplicate keys в source manifest не отклоняются.

### P1-4. Release test runner зависит от валидного inherited stdin

Все три production subprocess paths в
`scripts/build_release_test_evidence.py:120,153,517` используют `stdin=None`.
На Windows headless/pytest process с отсутствующим или invalid standard-input
handle падает в `DuplicateHandle` до запуска child command.

Synthetic invalid-stdin reproduction для `_run_environment_command()`:

```text
status failed
exit_code 125
error release_test_subprocess_failed:release_test_runner:OSError:...
```

Это соответствует уже наблюдавшимся `WinError 6/50` в объединённой матрице.
Standalone PTY run скрывает дефект, но release runner не должен требовать PTY.

### P1-5. Atomic writer сообщает failure после уже совершённого commit

`scripts/release_identity.py:1322` заменяет официальный path через `os.replace`,
после чего directory `fsync` на `:1329` всё ещё может выбросить исключение. Caller
получает `ReleaseEvidenceError`, хотя target уже изменён.

Synthetic post-replace failure:

```text
raised ReleaseEvidenceError
fsync_calls 2
sentinel_preserved False
published_payload {'schema_version': 'portable_path_migration_v1', 'status': 'passed'}
```

R18 test patch-ит весь `atomic_write_release_json` до записи и поэтому не проверяет
реальный post-commit failure.

## 3. Цель

1. Сохранить lexical filesystem boundary до любого resolve и проверить source и
   destination components.
2. Запретить чтение или запись через symlink/junction/reparse point при любом CLI
   пути исполнения redaction tool.
3. Связать backup и portable reports с immutable snapshot фактически проверенных
   входов и независимо проверять freshness в final verifier.
4. Сделать release subprocesses независимыми от terminal/stdin state.
5. Определить честную commit semantics atomic writer: caller всегда знает, была ли
   официальная публикация совершена.

## 4. Ограничения

- Не менять retrieval, enrichment, graph и card processing.
- Не запускать cold/live/final release workflow.
- Не запускать полный corpus/all-card run в рамках R19.
- Не ослаблять secret scan, identity envelope или fail-closed verifier.
- Не маскировать `WinError 6/50` требованием запускать pytest только в PTY.
- Не обещать rollback после `os.replace`, если его невозможно гарантировать.

## 5. Фаза 1. Сначала красные regression tests

### 5.1 Lexical source boundary

Добавить main-level tests для `security_artifact_redaction`:

- CLI `--root` указывает на file symlink;
- CLI `--root` указывает на directory junction/reparse point;
- link находится в parent component перед root;
- direct `scan()` и CLI `main()` используют один и тот же boundary helper;
- `_file_occurrences`, SQLite open и hash не вызываются после rejection;
- stdout/stderr/report не содержат raw target или secret marker.

Тест должен передавать lexical path в `main()`, а не заранее resolved fixture.

### 5.2 Destination boundary

Добавить tests для:

- junction/symlink directory внутри quarantine root;
- symlink target file в quarantine;
- reparse attribute без права создавать настоящий symlink;
- подмена обычного destination component на link между scan и copy;
- existing safe quarantine file с совпадающим hash как positive control.

Инварианты: внешний файл не создаётся и не меняется, source не редактируется,
remediation не публикует `status=passed`.

### 5.3 Evidence freshness

Backup matrix:

- source manifest bytes меняются после validation;
- backup file меняется после validation;
- duplicate `schema_version`, `files`, `path`, `bytes`, `sha256` отклоняются;
- final verifier отклоняет report, если frozen source manifest или current backup
  inventory не совпадает.

Portable matrix:

- candidate добавлен, удалён или изменён после scan;
- safe path заменён absolute/unresolved path;
- final verifier отклоняет старый report при inventory digest mismatch;
- одинаковый snapshot даёт одинаковый digest независимо от enumeration order.

### 5.4 Invalid stdin

Для `_run_environment_command`, `_pip_show_receipt` и каждого gate subprocess
подменить Windows standard-input handle на invalid. Все child commands должны
запуститься с `stdin=subprocess.DEVNULL` и дать обычные receipts.

Registry CLI tests R12 также должны быть независимы от stdin/PTY и проходить в
обычном pipe process.

### 5.5 Atomic commit point

Добавить fault-injection matrix по этапам:

- temporary open/write/file fsync;
- `os.replace` до commit;
- directory open/fsync/close после commit;
- cleanup temporary path;
- новый target и existing target.

Каждый result должен явно различать `not_committed` и `committed`. Нельзя проверять
только mock всего public writer.

## 6. Фаза 2. Исправление production code

### 6.1 Не терять lexical root

`main()` должен проверять raw `args.root` до `resolve()`. Shared path-boundary helper
принимает lexical root/candidate, проверяет каждый компонент через `lstat` и Windows
reparse attributes, затем отдельно вычисляет resolved path и containment.

Не передавать в `scan()` только resolved target без provenance исходного path.

### 6.2 Защитить quarantine destination

Перед collision check, hash и copy валидировать каждый destination component от
quarantine root до target. Повторить проверку непосредственно перед open/copy.

Для новых каталогов создавать компоненты по одному и проверять parent после каждого
шага. Если доступно, использовать descriptor/handle-based no-follow операции;
иначе оставить явный fail-closed Windows/POSIX helper и regression на TOCTOU.

### 6.3 Ввести snapshot contracts

Backup report должен как минимум содержать:

- `backup_manifest_sha256` по exact bytes;
- strict parse без duplicate keys;
- deterministic identity проверенных records/files;
- frozen source manifest как отдельный bundled evidence artifact.

Final verifier должен сверить report с bundled manifest и повторно выполнить pure
backup validation либо пересчитать inventory непосредственно перед финальным gate.

Portable report должен содержать deterministic inventory digest по relative path,
bytes и content hash всех candidates. Final verifier независимо пересчитывает
current digest и violations; одних counts/status недостаточно.

Обновить `REPORT_CONTRACTS`, run index/bundle inventory и negative verifier tests.

### 6.4 Сделать subprocess headless-safe

Во всех production `subprocess.run()` release runner установить
`stdin=subprocess.DEVNULL`. То же сделать в registry subprocess tests, где stdin не
является частью проверяемого поведения.

Не ловить invalid-handle error как обычный failed test command: child command в этом
случае вообще не был запущен.

### 6.5 Зафиксировать publication state machine

Определить commit point как успешный `os.replace` и разделить ошибки:

- до commit: target гарантированно не изменён, result `not_committed`;
- после commit: не утверждать, что publication отсутствует; вернуть/выбросить
  отдельный sanitized outcome с `committed=True` и запретить слепой retry;
- release-mode official paths по возможности сделать immutable/new-only;
- finalizer должен создавать отдельный failure/uncertain-durability receipt, не
  перезаписывая committed artifact.

Не пытаться гарантировать rollback после commit через best-effort обратный replace.

## 7. Проверки после исправлений

1. R19 regression tests отдельно.
2. Security redaction source/destination boundary matrix.
3. Backup/portable snapshot и final-verifier negative matrix.
4. Atomic writer fault-injection matrix.
5. R12 CLI subprocess matrix в обычном pipe, без PTY workaround.
6. R3 environment receipt и release test-gate tests в обычном pipe.
7. R10-R19 release/security targeted suite.
8. Ruff, compileall и `git diff --check` по R19 scope.
9. Общий unit/regression suite после зелёных targeted/static checks.
10. Новый read-only review результата; P0/P1/P2 должны отсутствовать.

Полный corpus/all-card run не является DoD R19. Он остаётся отложенным до конца
всего цикла исправлений и запускается только если финальное чистое review или
последние изменения действительно требуют его.

## 8. Definition of Done

- CLI не читает source через lexical root symlink/junction/reparse point.
- Quarantine remediation не пишет через link/reparse destination и не выходит за
  resolved quarantine root.
- Backup report cryptographically связан с exact source manifest и проверенным
  file inventory; duplicate JSON keys отклоняются.
- Portable report содержит deterministic candidate inventory digest, который final
  verifier пересчитывает.
- Release subprocess matrix проходит без PTY и без валидного inherited stdin.
- Atomic writer никогда не сообщает `not_committed` после успешного `os.replace`;
  post-commit durability failure имеет отдельное явное состояние.
- R19 targeted/static/common suites зелёные.
- Финальное read-only review не содержит P0/P1/P2.
- Release readiness не объявляется автоматически.

## 9. Запрещённые упрощения

- Не resolve-ить raw CLI path до link/reparse проверки.
- Не проверять только source, оставляя quarantine destination доверенным.
- Не добавлять один manifest hash без verifier-side recomputation/frozen artifact.
- Не считать status/counts доказательством freshness.
- Не лечить invalid stdin запуском только в PTY.
- Не подавлять post-commit fsync error так, будто durability подтверждена.

## 10. Результат исполнения

Исполнение завершено 2026-08-25 в основном потоке, без субагентов.

Реализовано:

- raw CLI root проверяется до resolve; source и quarantine destination запрещают
  symlink/junction/reparse components;
- quarantine copy использует exclusive/no-follow open и повторную проверку
  destination boundary;
- backup report содержит exact manifest hash и deterministic file inventory hash,
  source manifest включается в bundle отдельным evidence artifact;
- final verifier повторно проверяет current backup и portable snapshots;
- portable report содержит deterministic candidate inventory digest;
- release test subprocesses и R12 CLI probes используют `stdin=DEVNULL`;
- atomic writer различает pre-commit failure и post-commit
  `ReleasePublicationCommittedError(committed=True)`.

Проверки:

```text
R19:                    16 passed, 2 skipped
R10-R19 + contracts:   234 passed, 3 skipped
R3 pipe matrix:         52 passed
common suite:         1088 passed, 4 skipped, 42 subtests passed
ruff / compileall / diff --check: passed
```

Два R19 skip относятся только к созданию настоящего symlink без Windows
privilege; отдельные reparse-attribute regressions выполняются без skip. Финальный
прямой read-only review R19 scope не выявил P0/P1/P2.

Cold/live/final release workflow и полный corpus/all-card run не запускались.
