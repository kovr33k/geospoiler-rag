# ТЗ R21: pinned-path I/O, kernel lock и complete durable tree publication

Дата: 2026-08-26
Статус: **ИСПОЛНЕНО — RELEASE EXECUTION ПО-ПРЕЖНЕМУ BLOCKED**

## 1. Основание

После исполнения R20 выполнен новый прямой review текущего worktree без
субагентов. Проверялись release publication, security scan/redaction, lock
ownership и durability contracts.

Cold/live/final release workflow и полный corpus/all-card run не запускались.

## 2. Подтверждённые проблемы

### P1-1. Publication parent можно подменить после self-preflight

`scripts/build_release_evidence.py:_build_bundle()` проверяет и сканирует
`temporary_dir`, затем публикует его через строковые `source`/`target` paths.
Parent output-каталога не удерживается и не используется descriptor-relative
rename. Между self-preflight и `durable_replace()` parent можно заменить junction,
подложить другой каталог с тем же temporary name и получить `status=passed` для
чужого содержимого вне проверенного destination.

Детерминированный Windows reproduction:

```text
swap_happened=true
reported_passed=true
outside_published=true
validated_bundle_stranded=true
```

Тот же класс ошибки относится к final/failure directory publication и к POSIX
parent rename/symlink swap.

### P1-2. Source redaction может изменить файл вне artifacts root

`security_artifact_redaction.apply_remediation()` повторно проверяет source path,
но не удерживает source root/parent handles. Если source parent заменить junction
между validation и `_redact_text_file()`, запись выполняется по уже подменённому
пути. Следующая validation отклоняет операцию, но внешний файл уже изменён.

Детерминированный Windows reproduction:

```text
swap_attempted=true
rejected_after_write=true
outside_redacted=true
original_unchanged=true
passed_report_exists=false
```

Поздний fail-closed результат не отменяет состоявшуюся запись вне source boundary.

### P1-3. Security scan строит digest и verdict из разных физических снимков

`security_release_scan.scan()` сначала вызывает `_inventory_digest(candidates)`,
а затем повторно читает те же path через `_scan_path()`. Candidate path не pinned.
Файл можно временно заменить безопасным junction target только на время второго
чтения, затем вернуть исходный secret-bearing файл. Report получает digest исходных
байтов, но `status=passed` от безопасной подмены; freshness recomputation также
увидит исходные байты и не обнаружит обход.

Детерминированный Windows reproduction:

```text
swap_happened=true
status=passed
finding_files=0
secret_still_present=true
inventory_bound=true
```

Аналогичный split-read риск есть в `scan_release_artifact_tree()`.

### P1-4. JSON lock ownership не является атомарным

`build_release_evidence.build_bundle()` читает stale lock, затем отдельно renames
текущее имя. Между этими действиями другой writer может приобрести новый live lock;
первый процесс перемещает уже новый lock и продолжает сборку.

Reproduction stale-to-live takeover:

```text
build_proceeded=true
live_lock_was_quarantined=true
own_lock_cleaned=true
```

Cleanup также выполняет read-token, затем отдельный `unlink()`. Если новый writer
заменит lock после read, старый процесс удалит lock нового владельца:

```text
build_proceeded=true
takeover_happened=true
new_owner_lock_deleted=true
```

Существующий тест заменяет lock до cleanup read и поэтому не покрывает это окно.

### P1-5. Bundle не fsync-ит опубликованное дерево каталогов

`_build_bundle()` fsync-ит только regular files и затем вызывает
`durable_replace(temporary_dir, output_dir)`. POSIX primitive fsync-ит parent после
rename, но directory entries внутри temporary tree до rename не синхронизируются.
После power loss canonical attempt directory может существовать с неполным набором
entries, хотя producer уже вернул `passed`.

В final verifier локальный `_fsync_tree()` пытается fsync только root directory и
подавляет ошибку `os.open(directory)`. Nested directories не синхронизируются, а
pre-commit durability failure превращается в продолжение publication.

### P2-1. Failure artifact теряет committed-uncertain outcome

Если canonical final publication падает pre-commit, а публикация `failed-*`
каталога выполняет rename и затем сообщает durability error, generic catch подавляет
`ReleasePublicationCommittedError`. CLI печатает обычный `status=failed` и путь к
artifact без `committed=true`/uncertain state.

Fault injection:

```text
exit_code=1
failure_dir_committed=true
reported_status=failed
reported_committed=null
failure_path_reported=true
```

Это не создаёт ложный release pass, но нарушает единый publication outcome contract.

## 3. Цель

1. Все security-sensitive reads/writes и directory publications выполняются через
   pinned directory/file identity, а не повторное разрешение строкового path.
2. Security verdict и inventory digest вычисляются из одного immutable snapshot.
3. Lock ownership обеспечивается kernel primitive, без stale read/rename и
   read/unlink окон.
4. Directory tree полностью подготовлено к durable rename до commit.
5. Любой post-commit durability error сохраняет отдельный committed-uncertain
   outcome, включая secondary failure artifacts.

## 4. Реализация

### 4.1 Общий pinned-path слой

Добавить в `scripts/release_identity.py` общий внутренний API для закреплённых
каталогов и файлов.

- Windows: открыть root и каждый parent через `CreateFileW` с
  `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT`, без
  `FILE_SHARE_DELETE`; удерживать handles до завершения операции.
- POSIX: открыть chain через `os.open(..., O_DIRECTORY | O_NOFOLLOW)` и выполнять
  file creation/read/replace через `dir_fd` (`openat`/descriptor-relative
  `os.replace`).
- Final component не resolve-ить повторно. Source/temp/target identity проверять
  после захвата handles и перед commit.
- Temporary directory и canonical target должны находиться в одном pinned parent.
- Fail closed при unsupported descriptor semantics; не откатываться к строковым
  path operations.

Применить слой к:

- bundle temporary/final publication;
- final verifier canonical и failure directories;
- bundle recovery receipts и lock metadata;
- security redaction source/quarantine/report paths;
- `sync_media_contract` enriched-card writes (убрать deterministic shared temp name
  и локальный `Path.replace()` с подавлением parent fsync);
- `scan_release_artifact_tree()`.

### 4.2 Source redaction из pinned snapshot

- Закрепить artifacts root и source parent chain до первого source hash.
- Открыть source final component no-follow/reparse-aware.
- Initial hash, quarantine copy и redaction input получать из одного pinned source
  snapshot/handle.
- Redacted output строить во временном sibling внутри pinned parent и заменять final
  lexical name атомарно; никогда не следовать final symlink.
- SQLite source читать под тем же parent/file identity lock; temporary database и
  sidecars держать внутри приватного pinned staging directory.
- Post-redaction verification выполнять по опубликованному lexical name внутри того
  же pinned parent.

### 4.3 Single-read security snapshot

- Для каждого candidate создать immutable snapshot record один раз.
- Из одних и тех же snapshot bytes вычислять `bytes`, `sha256`, secret groups и
  report counts.
- `_inventory_digest()` должен принимать готовые snapshot records, а не повторно
  читать paths.
- SQLite анализировать из private snapshot copy, связанной с тем же record.
- После scan проверить, что pinned root/candidate identities не изменились; любое
  изменение даёт `status=failed` и stable sanitized error code.
- `scan_release_artifact_tree()` перевести на тот же single-read contract.

### 4.4 Kernel-backed writer lock

Заменить JSON-file ownership на kernel exclusive lock:

- Windows: `LockFileEx(..., LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY)`.
- POSIX: `fcntl.flock(fd, LOCK_EX | LOCK_NB)`.
- Lock file сделать постоянным coordination object; не unlink-ить его при release.
- Payload записывать/обновлять только после получения kernel lock.
- Если kernel lock получен, а предыдущий payload был `active`, записать sanitized
  recovery receipt: предыдущий процесс завершился без clean release.
- На clean release записать `status=released`, `released_at`, fsync file, затем
  unlock/close.
- PID/start marker оставить только diagnostic metadata, не использовать как
  ownership authority.

### 4.5 Complete durable tree preparation

Добавить общий `durable_prepare_tree(directory)` в `release_identity`.

- fsync каждого regular file;
- POSIX: fsync каждого nested directory bottom-up и root directory;
- Windows: fsync files, затем publication через `MoveFileExW(...WRITE_THROUGH)`;
- ошибка подготовки всегда pre-commit failure и запрещает canonical rename;
- не подавлять `open/fsync/close` errors;
- bundle, final verifier и failure artifacts используют один helper.

### 4.6 Honest secondary publication outcome

- Отдельно хранить `primary_failure` и `failure_artifact_publication`.
- `ReleasePublicationCommittedError` при публикации failure artifact не подавлять.
- CLI payload должен включать nested outcome с
  `status=committed_durability_uncertain`, `committed=true` и существующим path.
- Никогда не маркировать uncertain artifact как `not_committed` или обычный
  подтверждённый failure artifact.

## 5. Regression tests

1. Actual Windows junction swap output parent exactly before bundle rename:
   operation rejected, outside output отсутствует, validated temp не подменяется,
   `passed` не возвращается.
2. Такой же parent-swap contract для final и failure verifier directories.
3. Source parent junction swap exactly before text и SQLite redaction: outside bytes
   не меняются, source/quarantine/report остаются fail-closed.
4. Security scan swap/restore между inventory и analysis: report обязательно failed;
   digest и counts происходят из одного snapshot.
5. Аналогичный regression для `scan_release_artifact_tree()`.
6. Stale payload заменяется live owner перед recovery: новый writer не теряет lock,
   первый build не стартует.
7. Новый owner появляется между cleanup read и release: persistent kernel lock не
   удаляется и ownership не пересекается.
8. Два реальных процесса одновременно пытаются получить lock: ровно один входит в
   `_build_bundle()`.
9. POSIX tree-fsync call matrix содержит все nested directories bottom-up; любой
   fsync error блокирует rename.
10. Bundle и verifier используют общий `durable_prepare_tree` без локальных
    suppressing branches.
11. Failure artifact commit-then-error сохраняет nested committed-uncertain outcome.
12. Static guard запрещает direct path-based publication (`os.replace`,
    `Path.replace`, fixed shared temp names) в registered release producers, кроме
    внутренней реализации общего primitive.

## 6. Проверки

1. R21 targeted race/fault suite.
2. R19-R21 security/publication/lock matrix.
3. R10-R21 + release/security/backup contracts.
4. R3 lock/process tests в обычном pipe с внешним absolute `--basetemp`.
5. Ruff, compileall, `git diff --check`.
6. Общий unit/regression suite только после зелёных targeted/static checks.
7. Финальный прямой read-only review без P0/P1/P2.

Полный corpus/all-card run, cold/live и final release execution не входят в R21.
Они остаются отложенными до конца всего цикла, когда новый review не найдёт
P0/P1/P2 и исправлять больше нечего.

## 7. Definition of Done

- ни один проверенный source/destination parent нельзя подменить во время I/O;
- опубликованы именно те bytes/tree, которые прошли self-preflight и secret scan;
- security digest и verdict относятся к одному immutable snapshot;
- kernel lock не может быть украден или удалён другим writer;
- все directory entries durable до canonical commit;
- любой committed-uncertain outcome виден в CLI/receipt;
- targeted/static/common suites зелёные;
- финальный review не содержит P0/P1/P2.

## 8. Текущее evidence ревью

```text
R3 + R19 + R20 + security: 92 passed, 2 skipped
ruff:                       passed
git diff --check:           passed (только существующие CRLF/LF warnings)
new deterministic repros:   6 confirmed
```

Штатные тесты зелёные, но не покрывают перечисленные межоперационные окна.
До исполнения R21: **BLOCKED — DO NOT RUN RELEASE EXECUTION**.

## 9. Результат исполнения

Исполнено 2026-08-26 в основном потоке, без субагентов.

- Добавлен общий pinned-path слой для Windows/POSIX, включая handle-based rename
  точного Windows directory object и descriptor-relative POSIX operations.
- Bundle/final/failure publication, atomic JSON/card writes, redaction и artifact
  scan переведены на общие primitives.
- Redaction использует единый source snapshot для hash, quarantine и преобразования;
  SQLite обрабатывается через приватную snapshot-копию.
- Security scan формирует digest и verdict из одного `ScanSnapshot`.
- Writer coordination переведена на постоянный `LockFileEx`/`flock` lock без
  stale rename и cleanup unlink.
- Добавлен полный bottom-up `durable_prepare_tree()` и честный nested outcome для
  secondary failure publication.
- Добавлен `tests/test_release_evidence_r21.py` с race/fault/static regressions.

Фактические проверки:

```text
R21 targeted:                       13 passed
R3/R19-R21/security matrix:        105 passed, 2 skipped
accumulated release/security:      297 passed, 3 skipped
final affected matrix:             144 passed, 2 skipped
full unit/regression suite:         1117 passed, 4 skipped, 42 subtests passed
Windows directory rename repeat:   10/10 passed
ruff:                               passed
compileall:                         passed
git diff --check:                   passed (только существующие CRLF/LF warnings)
final direct review:                P0=0, P1=0, P2=0
```

Полный corpus/all-card run, cold/live и final release execution не выполнялись.
Они остаются отложенными до финала всего цикла проверок.
