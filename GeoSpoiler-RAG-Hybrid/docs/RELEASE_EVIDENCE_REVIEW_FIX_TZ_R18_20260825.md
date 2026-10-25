# ТЗ R18: fail-closed publication, inventory и artifact boundaries

Дата: 2026-08-25
Статус: **ИСПОЛНЕНО — RELEASE READINESS НЕ ОБЪЯВЛЕНА**

## 1. Основание

После исполнения R17 проведён новый read-only review текущего worktree и release
boundary. Проверка выполнялась в основном потоке, без субагентов и без полного
прогона карточек/corpus/release workflow.

Текущие targeted tests дают ложнозелёный результат:

```text
65 passed, 1 skipped
```

При этом отдельные synthetic reproductions подтвердили перечисленные ниже
дефекты.

## 2. Подтверждённые проблемы

### P1-1. Release producer публикует `passed` до identity stamping

`scripts/validate_backup_restore.py:189` записывает отчёт до вызова
`stamp_report()` на `:217`, а `scripts/migrate_portable_paths.py:271,291`
записывает отчёт до `stamp_report()` на `:303`.

Если stamping или последующая identity validation падает, официальный output path
уже содержит незапечатанный JSON со `status: passed`. Synthetic reproduction с
`stamp_report -> OSError` подтвердила:

```text
backup_unstamped_exists True
portable_unstamped_exists True
```

Это нарушает fail-closed контракт: release artifact должен появляться на
официальном пути только после полного формирования, stamping, secret scan и
валидации.

### P1-2. Security redaction следует за symlink/reparse point за пределы root

`scripts/security_artifact_redaction.py:205` выбирает все `item.is_file()` через
`root.rglob("*")`, не отклоняя symlink или Windows reparse point. Дальнейшие
чтение, hash и quarantine copy следуют к target.

Артефактный root поэтому может содержать ссылку на файл вне root; scanner прочитает
его, а remediation скопирует внешнее содержимое в quarantine. На Windows локальный
synthetic symlink test не смог создаться без привилегии (`WinError 1314`), но
статический путь исполнения однозначен, а текущие тесты такой boundary не
проверяют.

### P2-1. Inventory gate не обнаруживает helper schema-writer без `__main__`

`tests/test_release_evidence_r15.py:89-90` считает schema-only producer только при
наличии строкового literal `__main__`. Helper/module API, который напрямую пишет
official schema, но не является CLI, выпадает из discovery, если не импортирует
один из перечисленных shared helper tokens.

Подтверждённый synthetic result:

```text
helper_schema_writer_discovered False
```

Матрица `tests/test_release_evidence_r17.py:69-91` создаёт только fixtures с
`if __name__ == "__main__"`, поэтому закрепляет blind spot вместо проверки
требования «любой direct writer official schema».

### P2-2. Backup validator принимает schema-less manifest

`scripts/validate_backup_restore.py:88-92` разрешает `schema_version is None`.
Существующий tagged legacy contract уже представлен
`backup_restore_closeout_v1`; безымянный payload не нужен для совместимости и
превращает schema split в fail-open.

Причина сохранения исключения находится в test helper:
`tests/test_backup_restore.py:18-21` генерирует manifest без schema. Это тестовая
совместимость, а не зафиксированный production contract.

### P2-3. Sibling failure receipt теряет первичную причину interrupt

При `KeyboardInterrupt`/`CancelledError` и уже существующем canonical run dir
`scripts/build_release_verification.py:1935-1940` передаёт в
`_best_effort_verification_failure()` только secondary `preparation_error`.
Исходный interrupt object корректно перевыбрасывается, но durable receipt сообщает
только secondary `RuntimeError`.

Подтверждённый synthetic result:

```text
same_interrupt True
error_code verification_finalization_failed
exception_type RuntimeError
```

Для аудита receipt должен различать primary interruption и secondary publication
failure, не раскрывая raw exception text.

## 3. Цель

Закрыть release boundary так, чтобы:

1. официальный report path изменялся ровно один раз и только полностью stamped;
2. неуспешный stamping/scan/validation не создавал новый и не перезаписывал
   существующий официальный artifact;
3. artifact scanner никогда не читал и не копировал данные через
   symlink/junction/reparse point;
4. inventory находил direct writer official schema независимо от наличия CLI;
5. backup source manifest всегда имел явно разрешённый schema id;
6. durable interrupt receipt сохранял sanitized сведения и о primary, и о
   secondary failure, а затем перевыбрасывал тот же interrupt object.

## 4. Ограничения

- Не менять product retrieval, enrichment, graph или card processing.
- Не запускать cold/live/final release workflow.
- Не запускать полный прогон карточек или corpus в рамках R18.
- Не ослаблять secret scanning, immutable attempt semantics и identity checks.
- Не добавлять broad compatibility для schema-less JSON.
- Не перезаписывать существующий report при любой ошибке до commit publication.

## 5. Фаза 1. Сначала красные regression tests

### 5.1 Publication ordering matrix

Добавить parametrized tests минимум для:

- `scripts.validate_backup_restore`;
- `scripts.migrate_portable_paths` в режимах `scan`, `verify`, `apply`.

Для каждого producer проверить ошибки на этапах:

- `stamp_report`;
- release secret scan;
- final atomic writer.

Инварианты:

- новый output path отсутствует после ошибки до publication;
- существующий sentinel остаётся byte-identical;
- незапечатанный `status: passed` никогда не виден на official path;
- CLI возвращает/выбрасывает исходную sanitized ошибку по действующему safe CLI
  contract.

### 5.2 Symlink/reparse boundary

Добавить cross-platform tests:

- file symlink внутри root на target вне root;
- directory symlink/junction/reparse point внутри root;
- link/reparse component в глубине пути;
- обычный файл внутри root как positive control.

На Windows использовать проверку reparse attributes, а отсутствие права создавать
symlink не должно оставлять весь Windows contract непроверенным: допускается
monkeypatch `lstat()`/boundary helper для deterministic unit test плюс отдельный
integration test там, где ОС разрешает link creation.

До любого `_file_occurrences`, hash, SQLite open или quarantine copy внешний target
должен быть отклонён с безопасным диагностическим кодом.

### 5.3 Inventory gate

Добавить synthetic fixtures:

- module-level function пишет official schema без `__main__`;
- class method пишет official schema;
- `json.dump`, `json.dumps` + `write_text`, `write_bytes`;
- unknown/non-release schema остаётся отрицательным control;
- registered shared writer остаётся положительным control.

### 5.4 Backup schema

Добавить tests:

- `backup_source_manifest_v1` принимается;
- явно разрешённый tagged legacy schema принимается только read-only path;
- отсутствующий schema отклоняется;
- unknown schema отклоняется;
- report schema не принимается как новый source schema вне явного legacy adapter.

### 5.5 Interrupt receipt

Для `KeyboardInterrupt` и `asyncio.CancelledError` проверить сочетание с existing
run dir и другими preparation failures:

- перевыбрасывается тот же object;
- sibling failure artifact существует best effort;
- receipt содержит sanitized primary exception type/code/digest;
- secondary failure хранится отдельными sanitized полями;
- raw message/path marker отсутствует во всех observable channels.

## 6. Фаза 2. Исправление production code

### 6.1 Build, stamp, validate, publish once

Разделить построение payload и публикацию:

- `validate_backup_restore.validate()` должен строить/возвращать report без записи
  на release output path; если нужен standalone API с записью, дать ему отдельный
  явно названный wrapper;
- `migrate_portable_paths.scan/apply` не должны публиковать release report внутри
  business operation;
- `main()` выполняет preflight, operation, identity rebuild при mutation,
  `stamp_report`, secret scan и только затем один atomic write;
- при failure до final write официальный path не меняется.

Проверить registry-wide, что этот же anti-pattern не остался у других producer’ов.
Lifecycle/checkpoint/attempt-ledger artifacts разрешены только если они имеют
отдельный schema/contract и не могут быть приняты за final passed report.

### 6.2 Закрыть filesystem boundary redaction tool

Вынести или переиспользовать shared helper, который проверяет каждый path component
через `lstat()` и Windows `FILE_ATTRIBUTE_REPARSE_POINT` до чтения.

- Не следовать за symlink, junction, mount-like reparse point.
- Повторить проверку непосредственно перед hash/copy/remediation для защиты от
  TOCTOU в пределах разумного локального threat model.
- Убедиться, что resolved candidate остаётся внутри resolved root.
- Не включать raw внешний target в stdout/stderr/report diagnostic.

### 6.3 Сделать inventory semantic, а не CLI-dependent

Удалить обязательное условие `has_main`. Discovery должен опираться на сочетание
official schema literal/assignment и фактического writer/dataflow либо на единый
registry-declared writer API. Предпочтительно сократить эвристику: обязать producer
писать через shared release writer и отдельно AST-guard’ом запрещать direct write
official schemas вне allowlist.

### 6.4 Закрыть backup schema

Удалить `None` из accepted schemas. Обновить fixtures на
`backup_source_manifest_v1`. Tagged legacy schema оставить только как явно
документированный read-only adapter; новый producer не должен его выпускать как
source manifest.

### 6.5 Сохранить обе причины failure

Расширить sanitized failure receipt отдельными полями primary и secondary
diagnostic. Не сериализовать raw exception message/args/traceback. Ошибка записи
receipt остаётся подавленной как secondary, после чего исходный interrupt object
перевыбрасывается вне handlers.

## 7. Фаза 3. Проверки после исправлений

Порядок обязателен:

1. Новые R18 regression tests отдельно.
2. Затронутые backup/portable/security/final-verifier tests.
3. R15-R18 inventory и interrupt matrix.
4. Registry-driven CLI subprocess matrix.
5. Ruff по scope R18.
6. `compileall` по изменённым modules/tests.
7. `git diff --check`.
8. Один общий unit/regression suite только после зелёных targeted/static checks.
9. Независимое read-only review результата; P0/P1/P2 должны отсутствовать.

Полный corpus/all-card run здесь не является DoD. Он остаётся отложенным до конца
всего цикла исправлений и выполняется только если финальное чистое review или
последние изменения действительно требуют его.

## 8. Definition of Done

- Ни один final release report не публикуется до успешного stamping и validation.
- Ошибка до commit publication не создаёт и не меняет official output.
- Redaction scanner/remediator fail-closed отклоняет symlink и Windows reparse
  points до чтения внешнего target.
- Synthetic helper writer без `__main__` ломает inventory при отсутствии registry
  entry/allowlist.
- Schema-less backup manifest отклоняется.
- Interrupt receipt содержит sanitized primary и secondary diagnostics, а runtime
  перевыбрасывает тот же interrupt object.
- R18 targeted/static/common unit suites зелёные.
- Финальное read-only review не содержит P0/P1/P2.
- Release readiness не объявляется без отдельного финального решения.

## 9. Запрещённые упрощения

- Не удалять старый output после stamp failure: official path не должен появляться
  до final commit вообще.
- Не считать `Path.resolve()` достаточной защитой от symlink/reparse TOCTOU.
- Не skip-ать весь Windows reparse contract из-за отсутствия symlink privilege.
- Не заменять `__main__` другим filename/token heuristic.
- Не сохранять `None` schema только ради старого test helper.
- Не записывать raw primary/secondary exception text в failure receipt.

## 10. Результат исполнения

Исполнение выполнено 2026-08-25 в основном потоке, без субагентов.

Реализовано:

- backup и portable producers теперь строят payload без публикации и выполняют
  единственный final write после identity stamping;
- schema-less backup source manifests отклоняются, fixtures переведены на
  `backup_source_manifest_v1`, tagged legacy read-only payload сохранён;
- security redaction проверяет root и каждый candidate component через `lstat`,
  symlink и Windows reparse attributes до чтения и повторно перед remediation;
- schema inventory обнаруживает function/class/direct-byte writers official
  schemas без зависимости от `__main__`;
- verifier sibling receipt хранит раздельные sanitized `primary_failure` и
  `secondary_failure`, сохраняя повторный выброс исходного interrupt object.

Зафиксированный red baseline до исправлений:

```text
17 failed, 2 passed
```

Итоговые проверки:

```text
R18 regression matrix: 22 passed
R10-R18 + backup/security targeted matrix: 166 passed, 1 skipped
R12 CLI subprocess matrix (standalone PTY): 44 passed
R3 environment receipt (standalone PTY): 1 passed
Common suite excluding эти две отдельно проверенные subprocess-группы:
1032 passed, 2 skipped, 1 deselected, 42 subtests passed
Effective total: 1077 passed, 2 skipped, 42 subtests passed
Ruff R18 scope: passed
compileall R18 scope: passed
git diff --check R18 scope: passed (только line-ending warnings)
```

При объединении R3 environment receipt и R12 CLI matrix после других Windows
tests runner воспроизводит инфраструктурный `DuplicateHandle` (`WinError 6/50`)
до запуска child producer. Обе subprocess-группы прошли в отдельных свежих PTY
processes; production workaround в R18 не добавлялся.

Повторный read-only review R18 scope выполнен напрямую из-за запрета субагентов.
Новых P0/P1/P2 в изменениях R18 не обнаружено.

Cold/live/final release workflow и полный corpus/all-card run не выполнялись.
