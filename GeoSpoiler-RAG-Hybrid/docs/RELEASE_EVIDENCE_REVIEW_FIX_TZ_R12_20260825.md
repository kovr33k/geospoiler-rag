# ТЗ R12: безопасный argument parsing и полный accepted-loader error contract

Дата: 2026-08-25  
Статус: **ИСПОЛНЕНО — RELEASE READINESS НЕ ОБЪЯВЛЕНА**

## 1. Основание

Read-only review исполнения R11 подтвердило два дефекта уровня P1 и один
дефект уровня P2. Финальная R11 matrix проходит (`20 passed`), но проверяет
наличие `run_release_cli()` в producer source, а не реальные parser/error paths.

### P1-1. `argparse` публикует raw argv до safe CLI boundary

Все release producers создают обычный `argparse.ArgumentParser` внутри `main()`.
При malformed/unknown argument parser сначала пишет usage и исходные arguments
в stderr, затем поднимает `SystemExit(2)`. `run_release_cli()` ловит только
`Exception`, поэтому этот путь полностью обходит safe diagnostic.

Подтверждено реальным subprocess для `security_release_scan.py` с synthetic
secret-shaped unknown argument:

```text
returncode=2
marker_in_stdout=False
marker_in_stderr=True
```

Тот же класс ошибки относится ко всем 11 producer entry points и к invalid
`choices`/type conversion diagnostics, где `argparse` повторяет raw value.

### P1-2. Accepted loader начинает с raw `Path.resolve()`

`loader.ingest.load_from_manifest()` выполняет:

```python
path = Path(manifest_path).resolve(strict=False)
```

до shared safe path helper и до exception boundary. Synthetic `OSError` с
secret-shaped marker подтверждён в полном formatted traceback:

```text
exception=OSError
marker_in_traceback=True
```

Кроме того, несколько report-failure branches поднимают primary exception
непосредственно внутри `except`. `from None` скрывает context в стандартном
traceback, но не гарантирует detached `__context__` object во всей matrix.

### P2-1. Ошибка публикации FAILED report проглатывается

В manifest-read и `load_texts` failure paths исключение из
`_write_accepted_manifest_report()` ловится через `except Exception: pass`.
Наружу возвращается только primary load error.

Подтвержденный сценарий:

```text
primary_error=Cannot read accepted load manifest: OSError
report_publication_failure_visible=False
cause=None
context=None
```

Если старый accepted report уже существует, он может остаться без нового
durable FAILED evidence, а оператор не узнает из исключения, что публикация
отчета также завершилась ошибкой.

## 2. Цель

Закрыть parser/SystemExit channel до публикации raw argv и сделать все
accepted-manifest load failures единым detached safe contract, который не
теряет факт невозможности записать FAILED evidence.

## 3. Обязательные инварианты

1. Synthetic secret marker отсутствует в stdout, stderr, exception text,
   `__cause__`, `__context__` и `traceback.format_exception()`.
2. Invalid/unknown CLI arguments дают только compact safe diagnostic и non-zero
   exit code; raw argv и automatic argparse error text не публикуются.
3. `--help` остается штатным публичным путем с exit code `0`.
4. `KeyboardInterrupt` не превращается в обычную application error.
5. Accepted manifest path проходит lexical secret scan и safe resolution до
   чтения manifest или записи report.
6. Primary load failure остается различимым, но одновременная report
   publication failure также отражается stable code/type/message hash.
7. Ни один accepted-loader branch не поднимает public exception внутри `except`
   с raw lower-level error в `__context__`.
8. Preflight/parser rejection не меняет existing artifacts byte-for-byte и не
   запускает producer body.

## 4. Scope

Основной scope:

- `scripts/release_identity.py`;
- 11 release producer entry points, использующих `run_release_cli()`;
- `loader/ingest.py` только `load_from_manifest()` и accepted report errors;
- `tests/test_release_evidence_r11.py` либо новый R12 test module;
- `tests/test_manifest_loader.py`;
- соответствующие release/security/CLI regression tests.

Вне scope:

- retrieval, enrichment и Wiki business logic;
- scanner physical identity/counters, если regressions остаются зелёными;
- feature flags, providers, models и credentials;
- cold/live/final release execution;
- полный прогон корпуса и всех карточек;
- commit, push, PR и deploy.

## 5. Фаза 1. Safe parser boundary

1. Добавить shared parser/error mechanism, который предотвращает запись raw
   parser message в реальный stderr.
2. Допустимые реализации:

   - shared `ArgumentParser` subclass с безопасным `error()`;
   - controlled stderr capture внутри `run_release_cli()` с публикацией только
     после final secret scan;
   - иной общий механизм с теми же observable guarantees.

3. Не исправлять producers по одному разными локальными `try/except`.
4. Non-zero `SystemExit` от parser должен преобразовываться в stable structured
   diagnostic. `SystemExit(0)` от `--help` должен сохранять нормальное поведение.
5. Предусмотреть intentional non-zero producer completion отдельно от parser
   rejection, чтобы safe FAILED report не сопровождался raw traceback.
6. Любой buffered stderr перед публикацией проверить
   `assert_no_release_secrets()`; при finding исходный buffer отбросить.

## 6. Фаза 2. Accepted loader safe path

1. Заменить начальный bare `Path.resolve()` на
   `safe_resolve_release_path(..., label="accepted_manifest_path")`.
2. Lexical path проверять до resolve; resolved path проверять повторно.
3. Ошибка path construction/resolution должна возникать до manifest read,
   report write и LightRAG mutation.
4. Добавить synthetic resolve-error test для прямого library call и
   `python main.py load --manifest ...` CLI path.

## 7. Фаза 3. Detached load/report failure model

1. Для каждого primary failure branch сохранить два независимых результата:

   - primary load/validation error;
   - accepted report publication error либо `None`.

2. Не делать `raise` внутри `except`. Сначала построить safe diagnostic objects,
   выйти из exception handler, затем поднять detached public exception.
3. Если report publication завершилась ошибкой, public exception обязан
   содержать stable признак `accepted_manifest_report_publication_failed`,
   component, exception type и message hash без raw message.
4. Primary failure не заменять report error: оба факта должны быть различимы.
5. Не удалять и не перезаписывать existing report после failed publication
   попыткой «очистить» stale artifact. Existing bytes должны сохраниться;
   downstream обязан отвергать artifact по identity/run binding.
6. Синхронизировать manifest-read, shared-validation, snapshot-validation,
   `load_texts`, incomplete-load и success-report failure branches одним
   поведением.

## 8. Обязательные regression tests

### Real CLI parser matrix

Для каждого из 11 release producers запустить реальный subprocess с:

- минимальными safe required arguments;
- unknown argument, содержащим synthetic token;
- invalid choice/value с synthetic token, где применимо.

Проверить:

- non-zero exit;
- token отсутствует в stdout/stderr;
- stderr содержит один parseable safe diagnostic;
- `Traceback` и raw argparse `usage/error` отсутствуют;
- producer body mocks/side-effect sentinels не изменились.

Отдельно проверить `--help`: exit `0`, help доступен, safe wrapper не превращает
его в failure.

### Accepted loader matrix

- `Path.resolve()` бросает synthetic secret-bearing `OSError`;
- manifest read failure + report success;
- manifest read failure + report publication failure;
- shared validation failure + report publication failure;
- snapshot validation failure + report publication failure;
- `load_texts` failure + report publication failure;
- incomplete load + report publication failure;
- successful load + accepted report publication failure.

Для каждого public exception проверить `str`, `repr`, `__cause__`,
`__context__`, formatted traceback и сохранность existing report bytes.

Все tokens собирать внутри test body. Реальные credentials запрещены.

## 9. Порядок исполнения и проверки

1. Добавить failing R12 parser subprocess и loader error-matrix tests.
2. Реализовать shared parser boundary.
3. Перевести accepted loader на safe path helper.
4. Унифицировать detached primary/report failure diagnostics.
5. Запустить R12 negative matrix.
6. Запустить targeted R10/R11/R12, manifest, loader и release CLI suites.
7. Запустить расширенный release regression набор.
8. Ruff и compileall только затронутых Python files.
9. Выполнить `git diff --check HEAD --`.
10. Один общий unit/regression suite запускать только после зеленых targeted и
    static checks, поскольку меняется shared CLI/loader boundary.
11. Провести отдельный read-only review итогового diff.

**Полный корпус и все карточки не запускать.** Такой прогон остается последним
шагом всего проекта после завершения всех исправлений и review без P0/P1/P2
либо выполняется только когда последнее изменение нельзя подтвердить
targeted/static проверками.

## 10. Definition of Done

ТЗ выполнено, когда:

- оба P1 и P2 закрыты кодом и negative tests;
- malformed argv ни в одном producer не публикует synthetic marker;
- accepted loader использует shared safe path resolution;
- одновременная primary/report failure не теряется и не раскрывает raw errors;
- все exception objects detached от raw cause/context;
- existing artifacts неизменны при parser/preflight/publication rejection;
- targeted, expanded, static и общий unit/regression suite зелёные;
- отдельный review не содержит P0/P1/P2;
- cold/live/final release и all-card run не выполнялись;
- release readiness не объявлена.

## 11. Запрещенные упрощения

- проверять только прямой вызов `run_release_cli()` без реального parser
  subprocess;
- ловить `SystemExit` после того, как raw argparse text уже ушел в stderr;
- санитизировать только один producer parser;
- считать `from None` достаточным без проверки `__context__` object;
- продолжать `except Exception: pass` для report publication failures;
- заменять primary error report error либо наоборот;
- удалять stale report после failed write;
- подтверждать исправление cold/live/final release или прогоном карточек.

## 12. Результат исполнения

Исполнено 2026-08-25 в основном потоке без субагентов.

- добавлены shared `SafeArgumentParser` и buffered safe stderr boundary в
  `run_release_cli()`;
- все 11 release producers переведены на общий parser contract;
- parser rejection, intentional non-zero `SystemExit` и обычная producer error
  публикуются разными stable diagnostics;
- `--help` и прямой `SystemExit`-контракт parser сохранены;
- accepted manifest path проходит lexical/resolved safe validation до чтения
  manifest и до создания LightRAG в `main.py load` path;
- все failure branches accepted loader формируют detached public exception и
  не теряют `accepted_manifest_report_publication_failed`;
- fallback report больше не перезаписывает существующий artifact;
- добавлена `tests/test_release_evidence_r12.py` с real subprocess parser,
  invalid choice, help, stderr, `SystemExit`, direct loader, `main.py load`,
  loader branch matrix и existing artifact regressions.

Проверки:

- исходная CLI negative matrix: `11 failed` до исправления;
- финальная R12 matrix: `36 passed`;
- targeted R10/R11/R12/security/manifest: `101 passed`,
  `3 subtests passed`;
- расширенный release regression: `322 passed`, `2 skipped`,
  `3 subtests passed`;
- общий unit/regression suite: `946 passed`, `2 skipped`,
  `42 subtests passed`;
- Ruff, compileall и `git diff --check HEAD --`: passed; сохранены только
  существующие предупреждения Git о будущей CRLF/LF normalization.

Финальный read-only review R12 scope не выявил P0/P1/P2. Полный корпус, все
карточки, cold/live/final release, commit, push и deploy не выполнялись.
Release readiness этим ТЗ не объявляется.
