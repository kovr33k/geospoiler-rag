# ТЗ R13: exception diagnostics без вызова untrusted rendering

Дата: 2026-08-25  
Статус: **ИСПОЛНЕНО — RELEASE READINESS НЕ ОБЪЯВЛЕНА**

## 1. Основание

Независимый read-only review исполнения R12 выявил два дефекта уровня P1:
общий дефект в `scripts.release_identity.safe_exception_diagnostic()` и
повторную публикацию raw exception type в cold failure report.

Финальная R12 matrix проходит (`36 passed`), но использует исключения с обычным
`__str__` и безопасными именами классов. Два hostile exception сценария не
проверяются.

### P1-1. Диагностика вызывает untrusted `str(exc)`

Helper вычисляет `message_sha256` через:

```python
hashlib.sha256(str(exc).encode(...))
```

Exception rendering является исполняемым Python-кодом. Если `__str__` бросает
новое исключение, оно возникает внутри safe boundary и заменяет исходную
диагностику.

Подтверждено synthetic exception, чей `__str__` бросает secret-bearing
`OSError`:

```text
run_release_cli: escaped=OSError, marker_in_traceback=true
safe_resolve_release_path: escaped=OSError, marker_in_traceback=true
accepted loader failure builder: escaped=OSError, marker_in_traceback=true
```

У CLI и path resolver новый exception также сохраняет исходный hostile
exception в `__context__`.

### P1-2. `exception_type` публикуется без secret scan

`type(exc).__name__` копируется в diagnostic напрямую. Python допускает
динамически созданный exception class с произвольным именем.

Реальный вызов `run_release_cli()` с synthetic secret-shaped class name дал:

```text
return_code=1
marker_in_stderr=true
error_code=release_cli_failed
```

Тот же helper используется в path resolution, report publication, accepted
loader, cold graph, smoke, release evidence, release verification и test
evidence. Поэтому дефект общий, а не локальный для одного producer.

### P1-3. Cold report повторно вставляет raw exception type

Даже после построения shared diagnostic `_failed_report()` записывает отдельное
поле:

```python
"exception_type": type(exc).__name__
```

В non-release режиме итоговый report не проходит финальный
`assert_no_release_secrets()` и публикуется через локальный
`_atomic_write_report()`.

Подтверждено direct failure-report сценарием с synthetic secret-shaped class:

```text
marker_in_report=true
exception_type=synthetic secret-shaped class name
```

Следовательно, исправления только shared helper недостаточно: consumers не
должны повторно извлекать raw metadata после safe diagnostic.

## 2. Цель

Сделать exception diagnostic чистой fail-closed операцией, которая не вызывает
`str()`, `repr()` или пользовательский formatter исключения и не публикует ни
одно динамическое поле без secret scan.

## 3. Обязательные инварианты

1. Построение diagnostic не вызывает `str(exc)`, `repr(exc)` и форматирование
   traceback.
2. Exception с throwing или side-effecting `__str__` не может изменить stdout,
   stderr, artifacts или control flow safe boundary.
3. Synthetic marker отсутствует в `error_code`, `component`, `exception_type`,
   `message_sha256`, serialized diagnostic и публичном traceback.
4. `safe_exception_diagnostic()` всегда возвращает объект фиксированной схемы
   для любого `BaseException`, включая hostile subclasses.
5. `KeyboardInterrupt`, возникший как реальное управляющее событие producer,
   по-прежнему не превращается в application failure.
6. Public exceptions остаются detached: `__cause__ is None` и
   `__context__ is None`.
7. Stable fingerprint остается детерминированным для одинаковых безопасно
   извлечённых primitive args.
8. Cold failure report использует только поля shared safe diagnostic и проходит
   secret scan также без release identity preflight.

## 4. Scope

Основной scope:

- `scripts/release_identity.py`;
- `loader/ingest.py` только integration с shared diagnostic;
- consumers `safe_exception_diagnostic()` в release/cold/smoke/test-evidence
  scripts только если shared fix требует адаптации;
- `scripts/cold_graph_check.py` failure report и publication boundary;
- новый `tests/test_release_evidence_r13.py` либо расширение R12 tests;
- release identity, accepted loader и CLI regressions.

Вне scope:

- изменение producer business logic;
- retrieval, enrichment и Wiki;
- scanner physical identity;
- cold/live/final release execution;
- полный корпус и все карточки;
- commit, push, PR и deploy.

## 5. Фаза 1. Safe exception fingerprint

1. Убрать `str(exc)` из `safe_exception_diagnostic()`.
2. Строить `message_sha256` из безопасного структурного представления:

   - primitive `BaseException.args`: `None`, bool, int, finite/non-finite float,
     str и bytes;
   - для container/non-primitive values использовать только стабильный type
     tag и bounded structural shape;
   - не вызывать пользовательские `__str__`, `__repr__`, iterator, property или
     serializer hooks.

3. Ограничить depth, item count и byte count fingerprint input.
4. При невозможности безопасно прочитать args использовать фиксированный
   fallback fingerprint material, не пытаться отрендерить secondary exception.
5. Сохранить ключ `message_sha256`, если смена schema не нужна; документировать,
   что это safe structural fingerprint, а не обязательно SHA-256 от `str(exc)`.

## 6. Фаза 2. Safe dynamic fields

1. Нормализовать `exception_type` без вызова пользовательских formatters.
2. Пропустить `exception_type`, `error_code` и `component` через финальный
   `assert_no_release_secrets()` либо заменить небезопасное поле fixed sentinel.
3. Ограничить длину и допустимый алфавит type/component fields.
4. Перед возвратом проверить serialized canonical diagnostic целиком.
5. Не включать raw class name, module name, exception args или fallback error
   message в public output.
6. Удалить повторное `type(exc).__name__` из cold failure report и брать
   `exception_type` только из shared diagnostic.
7. Проверять canonical cold report перед любой publication, включая
   non-release failure path.

## 7. Обязательные regression tests

### Hostile rendering matrix

- `__str__` бросает secret-bearing `OSError`;
- `__str__` бросает `RuntimeError` с nested hostile cause;
- `__str__` пишет synthetic marker в stdout/stderr и затем возвращает строку;
- `__repr__` бросает либо пишет marker;
- args содержат object с hostile `__str__`, `__repr__` и iterator;
- args содержат nested containers, bytes, Unicode и cyclic structure.

Для каждого сценария проверить, что hostile hooks не вызывались либо не дали
observable side effect.

### Dynamic metadata matrix

- exception class name содержит synthetic token;
- class name содержит newline/control characters и имеет длину больше лимита;
- component/label содержит synthetic token при прямом library call;
- error code содержит synthetic token при прямом library call.

### Consumer matrix

Проверить hostile exception через:

- прямой `safe_exception_diagnostic()`;
- `safe_release_exception()`;
- `safe_resolve_release_path()`;
- `atomic_write_release_json()` и text publication;
- `run_release_cli()`;
- accepted loader primary + report failure;
- cold `_failed_report()` с release preflight и без него;
- по одному representative consumer для smoke/release evidence.

Для public results проверить stdout, stderr, `str`, `repr`, `__cause__`,
`__context__`, `traceback.format_exception()` и serialized artifact/report.

Все synthetic tokens создавать внутри test body. Реальные credentials
запрещены.

## 8. Порядок исполнения и проверки

1. Добавить failing R13 hostile exception tests.
2. Реализовать bounded structural exception fingerprint.
3. Закрыть dynamic diagnostic fields финальным secret scan.
4. Перевести cold failure report на shared safe metadata и unconditional scan.
5. Запустить R13 negative matrix.
6. Запустить targeted R11/R12/R13, loader, identity, CLI и publication suites.
7. Запустить расширенный release regression набор.
8. Ruff и compileall только затронутых Python files.
9. Выполнить `git diff --check HEAD --`.
10. Один общий unit/regression suite запускать только после зелёных targeted и
   static checks, поскольку меняется shared diagnostic helper.
11. Провести отдельный read-only review итогового diff.

**Полный корпус и все карточки не запускать.** Этот прогон остаётся последней
проверкой всего проекта после завершения исправлений и review без P0/P1/P2 либо
выполняется только если последнее изменение нельзя подтвердить targeted/static
проверками.

## 9. Definition of Done

ТЗ выполнено, когда:

- hostile rendering и dynamic type exploit-пути закрыты общим helper;
- cold report не возвращает raw exception metadata;
- hostile rendering hooks не вызываются диагностикой;
- ни одно dynamic diagnostic field не публикует synthetic marker;
- все consumers получают detached stable diagnostic;
- targeted, expanded, static и общий unit/regression suite зелёные;
- отдельный review не содержит P0/P1/P2;
- cold/live/final release и all-card run не выполнялись;
- release readiness не объявлена.

## 10. Запрещённые упрощения

- оборачивать `str(exc)` ещё одним `try/except` и затем рендерить secondary
  exception;
- использовать `repr(exc)` как fallback;
- исправлять только `run_release_cli()`;
- считать class name доверенным только потому, что это metadata;
- проверять только stderr без exception chain и artifacts;
- ловить реальный `KeyboardInterrupt` producer как обычную ошибку;
- подтверждать исправление cold/live/final release или прогоном карточек.

## 11. Результат исполнения

Исполнено 2026-08-25 без субагентов.

Реализовано:

- `message_sha256` переведён на bounded structural fingerprint без вызова
  `str()`, `repr()`, пользовательских iterator/property/serializer hooks;
- primitive и bounded container args кодируются детерминированно, циклы и
  лимиты обрабатываются фиксированным образом, non-primitive значения получают
  opaque type tag;
- `exception_type`, `error_code` и `component` нормализуются, ограничиваются и
  проходят итоговый secret scan вместе со всем diagnostic;
- shared consumers в release identity и cold graph больше не публикуют raw
  exception type;
- cold report сериализуется и проходит unconditional secret scan до создания
  каталога, temporary file и замены существующего report.

Доказательства:

- исходная hostile R13 matrix до исправления: `9 failed`;
- финальная R13 matrix: `17 passed`;
- targeted R10/R11/R12/R13 и связанные identity/evidence/cold/security suites:
  `148 passed, 3 subtests passed`;
- дополнительная проверка environment snapshot и artifact scanner:
  `38 passed, 38 deselected`;
- расширенный release regression набор:
  `338 passed, 2 skipped, 3 subtests passed`;
- общий unit/regression suite после зелёных targeted/static checks:
  `962 passed, 2 skipped, 42 subtests passed`;
- Ruff, compileall затронутых файлов и `git diff --check HEAD --` прошли;
- финальный read-only review итогового R13 diff: P0/P1/P2 не обнаружены.

Полный корпус и все карточки, cold/live/final release, commit, push, PR и deploy
не выполнялись. Release readiness не объявлена.
