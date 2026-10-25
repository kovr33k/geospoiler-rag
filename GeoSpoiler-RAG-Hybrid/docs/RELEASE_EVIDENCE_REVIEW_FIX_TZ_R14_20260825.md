# ТЗ R14: закрытие exception metadata во всех release consumers

Дата: 2026-08-25  
Статус: **ИСПОЛНЕНО — RELEASE READINESS НЕ ОБЪЯВЛЕНА**

## 1. Основание

Независимый read-only review исполнения R13 выявил два дефекта уровня P1 и
один дефект уровня P2. Ревью выполнено без субагентов. Полный корпус, карточки,
cold/live и final release не запускались.

Текущий targeted baseline остаётся зелёным:

```text
43 passed, 3 subtests passed
```

Однако существующие тесты не проходят через несколько реальных consumer paths.

### P1-1. Accepted loader повторно публикует raw exception type

`loader/ingest.py` сохраняет отдельный helper:

```python
def _safe_report_error(prefix: str, exc: BaseException) -> str:
    return f"{prefix}: {type(exc).__name__}"
```

Он используется в preparation/write/fallback failure paths. Synthetic exception
с secret-shaped class name, брошенный atomic writer после успешной fallback
записи, дал:

```text
escaped_type=AcceptedManifestReportError
marker_in_public_error=true
cause_is_none=true
context_is_none=true
fallback_report_exists=true
```

Таким образом, exception chain отсоединена, но public message всё равно содержит
raw dynamic metadata.

В том же модуле прямой `type(exc).__name__` остаётся в path construction,
manifest read и load failure paths.

### P1-2. Release producers обходят shared diagnostic

Прямой `type(exc).__name__` остаётся в release-bound consumers:

- `loader/manifest_contract.py`;
- `scripts/build_release_verification.py`;
- `scripts/security_release_scan.py`;
- `scripts/build_release_test_evidence.py`;
- `scripts/sync_media_contract.py`;
- `scripts/validate_backup_restore.py`;
- `scripts/migrate_portable_paths.py`.

Часть этих значений попадает в report до финального secret scan. В
`security_release_scan.scan()` synthetic discovery exception дал:

```text
status=failed
marker_in_report=true
final_publication_scan_rejects_report=true
```

Секрет не записывается, но producer теряет обязательный failed report. Это
fail-closed относительно утечки, но не выполняет контракт durable failure
evidence и инвариант R13 о shared safe metadata.

### P2-1. Fingerprint dict зависит от insertion order

`_encode_exception_fingerprint_value()` кодирует exact dict через
`dict.items(value)` в порядке вставки. Два равных primitive dict args дали:

```text
equal_dict_args=true
fingerprints_equal=false
```

Следовательно, `message_sha256` не является каноническим для логически
одинаковых безопасных аргументов.

## 2. Цель

Закрыть все обходы shared exception diagnostic на release-evidence boundary и
сделать bounded structural fingerprint каноническим для unordered exact
containers, не вызывая пользовательские hooks.

## 3. Обязательные инварианты

1. Ни один release-bound consumer не публикует `type(exc).__name__`, `str(exc)`,
   `repr(exc)` или raw exception args.
2. Public exceptions, stderr и reports содержат только значения, полученные из
   `safe_exception_diagnostic()` либо фиксированные literals.
3. Hostile dynamic class name не попадает в public traceback или artifact и не
   мешает публикации безопасного failed report.
4. Public exceptions остаются detached: `__cause__ is None` и
   `__context__ is None`.
5. Равные exact dict/frozenset/set args дают одинаковый fingerprint независимо
   от порядка вставки или итерации.
6. Разные bounded primitive contents по возможности дают разные fingerprints.
7. List и tuple сохраняют порядок как часть fingerprint semantics.
8. Depth, item и byte limits остаются жёсткими; hostile hooks не вызываются.
9. Реальный `KeyboardInterrupt` producer не превращается в обычную application
   failure, кроме уже документированных durable-receipt boundaries.
10. Любой rejected payload оставляет существующий artifact неизменным.

## 4. Scope

Основной scope:

- `scripts/release_identity.py`;
- `loader/ingest.py`;
- `loader/manifest_contract.py`;
- `scripts/build_release_verification.py`;
- `scripts/security_release_scan.py`;
- `scripts/build_release_test_evidence.py`;
- `scripts/sync_media_contract.py`;
- `scripts/validate_backup_restore.py`;
- `scripts/migrate_portable_paths.py`;
- новый `tests/test_release_evidence_r14.py` и точечные существующие tests;
- R11/R12/R13 regression suites.

Вне scope:

- обычный retrieval/late-fusion runtime и enrichment diagnostics, не входящие в
  release-evidence publication boundary;
- изменение producer business logic и report schemas, кроме замены unsafe
  diagnostic values;
- cold/live/final release execution;
- полный корпус и все карточки;
- commit, push, PR и deploy.

## 5. Фаза 1. Единый consumer-safe diagnostic

1. Удалить `_safe_report_error()` из `loader/ingest.py` либо перевести его на
   `safe_exception_diagnostic()` без raw metadata.
2. Для строковых error fields использовать единый компактный formatter поверх
   shared diagnostic, например из `error_code`, `exception_type` и
   `message_sha256`.
3. Formatter должен принимать fixed safe prefix/code, нормализовать dynamic
   component и не рендерить exception.
4. Перевести accepted loader preparation/write/fallback/path/read/load failures
   на shared diagnostic.
5. Перевести перечисленные release producers и manifest validator на тот же
   контракт.
6. Не импортировать private `_safe_exception_type()` в consumers и не создавать
   локальные копии sanitization logic.
7. После замены выполнить статический guard по scope-файлам: прямые
   `type(exc).__name__`, `str(exc)` и `repr(exc)` запрещены.

## 6. Фаза 2. Канонический bounded fingerprint

1. Сохранить порядок list/tuple.
2. Для exact dict с числом элементов не больше лимита:

   - кодировать каждую пару key/value в изолированный bounded byte block;
   - сортировать готовые blocks лексикографически;
   - объединять только после сортировки;
   - не сравнивать сами Python keys и не вызывать их hooks.

3. Для exact set/frozenset с числом элементов не больше лимита аналогично
   сортировать безопасно закодированные element blocks.
4. Для containers больше item limit использовать канонический shape-only
   material с type, length и fixed truncation marker. Нельзя выбирать первые N
   элементов из unordered container, поскольку это снова зависит от iteration
   order.
5. Сохранить cycle detection и общий byte/depth limits.
6. Encoding failure должен давать fixed fallback fingerprint и не рендерить
   secondary exception.

## 7. Обязательные regression tests

### Accepted loader

- atomic writer бросает exception с secret-shaped class name, fallback report
  успешно создаётся, public error не содержит marker;
- preparation failure, fallback-write failure, path construction, manifest read
  и load failure не содержат raw type;
- `str`, `repr`, `traceback.format_exception`, stdout/stderr и exception chain
  проверяются отдельно.

### Release consumer matrix

Для каждого scope producer добавить минимум один hostile exception path:

- manifest snapshot/read;
- security candidate discovery/analysis;
- final verifier corpus/storage/reconciliation path;
- test-evidence subprocess diagnostic;
- media contract input;
- backup restore path validation;
- portable path migration read/JSON failure.

Проверить:

- marker отсутствует в in-memory report, serialized report и public exception;
- safe failed report публикуется, когда publication path доступен;
- финальный secret scan принимает безопасный failed report;
- существующий artifact не меняется при реальном scanner rejection.

### Fingerprint matrix

- равные dict с разным insertion order;
- nested равные dict;
- set/frozenset с разным construction order;
- разные bounded primitive dict/set contents;
- unordered container больше item limit;
- cycle через dict/list;
- hostile key/value objects с `__str__`, `__repr__`, `__iter__`, `__eq__` и
  ordering hooks;
- повторный вызов и отдельные равные exception instances дают одинаковый hash.

Все synthetic markers создавать только внутри test body. Реальные credentials
запрещены.

## 8. Порядок исполнения и проверки

1. Добавить failing R14 tests для трёх подтверждённых дефектов.
2. Исправить canonical unordered-container fingerprint.
3. Ввести или переиспользовать единый safe diagnostic formatter.
4. Перевести accepted loader и manifest validator.
5. Перевести остальные release-bound consumers из scope.
6. Запустить R14 negative matrix.
7. Запустить targeted R11/R12/R13/R14, manifest loader, security scan,
   test-evidence, verifier и publication suites.
8. Выполнить source guard на запрещённые raw exception patterns в scope-файлах.
9. Запустить расширенный release regression набор.
10. Ruff и compileall только затронутых Python files.
11. Выполнить `git diff --check HEAD --`.
12. Один общий unit/regression suite запустить только после зелёных targeted и
    static checks, поскольку меняется shared boundary.
13. Провести отдельный read-only review итогового diff.

**Полный корпус и все карточки не запускать.** Этот прогон остаётся последней
проверкой всего проекта после завершения исправлений и review без P0/P1/P2 либо
выполняется только если последнее изменение невозможно подтвердить
targeted/static проверками.

## 9. Definition of Done

ТЗ выполнено, когда:

- три review reproductions сначала падают на старом коде и проходят после fix;
- accepted loader не публикует raw dynamic exception metadata;
- все release-bound consumers из scope используют shared safe diagnostic;
- безопасный failed report не блокируется scanner из-за exception class name;
- unordered exact containers имеют канонический bounded fingerprint;
- hostile hooks не вызываются;
- source guard, targeted, expanded, static и общий unit/regression suite зелёные;
- отдельный review не содержит P0/P1/P2;
- cold/live/final release и all-card run не выполнялись;
- release readiness не объявлена.

## 10. Запрещённые упрощения

- исправлять только `_safe_report_error()`;
- надеяться, что финальный scanner заменяет consumer sanitization;
- ловить scanner rejection и публиковать исходный payload другим writer;
- сортировать raw dict keys/set values;
- вызывать `str`, `repr`, comparison или iterator hooks ради canonical order;
- выбирать первые N элементов большого unordered container;
- менять release schemas без необходимости;
- подтверждать исправление cold/live/final release или прогоном карточек.

## 11. Результат исполнения

Исполнено 2026-08-25 без субагентов.

Реализовано:

- добавлен общий `safe_exception_token()` поверх
  `safe_exception_diagnostic()` для строковых error fields;
- accepted loader, manifest validator и все release-bound consumers из scope
  переведены с raw `type(exc).__name__` на shared safe diagnostic;
- synthetic exception metadata больше не попадает в public loader exception и
  не блокирует публикацию safe security failed report;
- exact dict/set/frozenset до item limit кодируются в независимые byte blocks и
  сортируются после безопасного encoding;
- большие unordered containers получают order-independent shape-only marker;
- list/tuple order, cycle detection и общие depth/item/byte limits сохранены;
- добавлен source guard на прямые raw exception patterns в scope-файлах.

Доказательства:

- исходная R14 negative matrix: `4 failed`;
- финальная R14 hostile/consumer/fingerprint matrix: `11 passed`;
- targeted R11/R12/R13/R14 и связанные manifest/security/verifier/backup/media
  suites: `126 passed, 1 skipped, 3 subtests passed`;
- расширенный release regression набор:
  `303 passed, 2 skipped, 3 subtests passed`;
- общий unit/regression suite после зелёных targeted/static checks:
  `973 passed, 2 skipped, 42 subtests passed`;
- Ruff, compileall, source guard и `git diff --check HEAD --` прошли;
- финальный read-only review итогового R14 diff: P0/P1/P2 не обнаружены.

Полный корпус и все карточки, cold/live/final release, commit, push, PR и deploy
не выполнялись. Release readiness не объявлена.
