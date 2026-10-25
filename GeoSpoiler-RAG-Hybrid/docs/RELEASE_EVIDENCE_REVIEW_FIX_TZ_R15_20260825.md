# ТЗ R15: AST-guard exception rendering и полный release boundary

Дата: 2026-08-25  
Статус: **ИСПОЛНЕНО — RELEASE READINESS НЕ ОБЪЯВЛЕНА**

## 1. Основание

Независимый read-only review исполнения R14 выявил два дефекта уровня P1 и
один дефект уровня P2. Ревью выполнено в основном потоке без субагентов.

Текущая R14 matrix остаётся зелёной:

```text
11 passed
```

Однако regex source guard проверяет только явные `str(exc)`, `repr(exc)` и
`type(exc).__name__`. Он не распознаёт неявный вызов `__str__` внутри f-string.

### P1-1. F-string обходит R14 source guard

В release-bound коде остались конструкции вида:

```python
raise ValueError(f"cannot read cold graph checkpoint: {exc}") from exc
```

Они найдены в:

- `loader/ingest.py`;
- `scripts/cold_graph_check.py`;
- `scripts/cold_provenance.py`;
- `scripts/luna_reenrich_audit.py`.

Synthetic `OSError` с side-effecting `__str__` через
`cold_graph_check._read_checkpoint()` дал:

```text
calls=["str", "str"]
marker_in_stdout=true
marker_in_traceback=true
escaped_type=ValueError
context_is_none=false
```

Первый вызов произошёл при построении f-string, второй при форматировании
traceback. Значит hostile rendering снова может менять observable output и
exception chain до shared safe boundary.

### P1-2. Release producer/dependency inventory неполный

R14 guard содержит ручной список файлов и не включает транзитивные release
dependencies. AST-проверка 16 фактических release producers/dependencies нашла
13 raw rendering paths, хотя R14 regex guard сообщает clean.

Кроме того, `scripts/build_release_manifest.py` создаёт
`accepted_manifest_v2`, но не использует `run_release_cli()`, safe publication
helpers и общий exception diagnostic. Он также публикует `f"...:{exc}"` в
public `RuntimeError`. В репозитории не найдено активных ссылок на этот producer,
поэтому его необходимо либо безопасно включить в официальный release boundary,
либо явно вывести из эксплуатации.

### P2-1. Opaque type tag различает только `__name__`

`_opaque_type_fingerprint()` хеширует только class `__name__`. Два разных типа
`first_module.SameOpaque` и `second_module.SameOpaque` дали одинаковый
`message_sha256`:

```text
distinct_types=true
same_name=true
fingerprints_equal=true
```

Это создаёт систематическую коллизию для разных opaque types с одинаковым
именем.

## 2. Цель

Запретить любое неявное и явное rendering исключений во всём release boundary,
сделать inventory проверяемым и устранить коллизию opaque type tags без
публикации raw module/class metadata.

## 3. Обязательные инварианты

1. Release-bound exception handlers не вызывают `str`, `repr`, `format`,
   f-string formatting или `%`/`.format()` с объектом исключения.
2. Hostile `__str__`, `__repr__`, `__format__` и traceback hooks не вызываются
   producer/dependency кодом.
3. Synthetic marker отсутствует в stdout, stderr, public exception,
   `__cause__`, `__context__`, traceback и artifacts.
4. Public exceptions остаются detached.
5. Все official release producers и их direct diagnostic dependencies входят в
   один проверяемый inventory.
6. Новый producer нельзя добавить, не включив его в AST guard.
7. Разные opaque types с разными безопасно извлечёнными module/qualname identity
   получают разные hashed type tags.
8. Raw module, qualname и class name никогда не публикуются.
9. Fingerprint остаётся bounded и не вызывает metaclass/user hooks.
10. Реальный `KeyboardInterrupt` не превращается в обычную application failure.

## 4. Scope

Основной scope:

- `scripts/release_identity.py`;
- `tests/test_release_evidence_r14.py` и новый
  `tests/test_release_evidence_r15.py`;
- `loader/ingest.py`;
- `scripts/cold_graph_check.py`;
- `scripts/cold_provenance.py`;
- `scripts/luna_reenrich_audit.py`;
- `scripts/build_release_manifest.py`;
- официальный inventory release producers/dependencies;
- все файлы, обнаруженные AST guard как содержащие unsafe rendering внутри
  official release boundary.

Вне scope:

- обычные query/late-fusion/storage runtime logs, не используемые release
  evidence producers;
- изменение retrieval, enrichment и Wiki business logic;
- cold/live/final release execution;
- полный корпус и все карточки;
- commit, push, PR и deploy.

## 5. Фаза 1. AST-aware source guard

1. Заменить regex-only guard на AST traversal.
2. Для каждого `except ... as name` обнаруживать внутри handler:

   - `str(name)`, `repr(name)`, `format(name)`;
   - `f"{name}"`, включая conversion `!s`, `!r`, `!a` и format spec;
   - `"{}".format(name)` и `% name`;
   - прямое чтение `name.args`;
   - `type(name).__name__` и эквивалентные raw metadata paths;
   - передачу exception в logger/print без предварительного safe diagnostic;
   - `raise ... from name`, если public exception до этого содержит rendered
     metadata или boundary требует detached exception.

3. Guard должен сообщать `file:line:kind` для каждого нарушения.
4. Inventory должен строиться из явного checked-in списка official producers и
   direct diagnostic dependencies, а не из вручную выбранного test tuple.
5. Добавить completeness test: каждый Python CLI, использующий
   `run_release_cli`, `atomic_write_release_json/text` либо создающий официальный
   release artifact schema, присутствует в inventory.

## 6. Фаза 2. Consumer closure

1. Заменить raw f-string rendering в `loader/ingest.py` на
   `safe_exception_token()` или fixed error code.
2. Перевести checkpoint, manifest и post-run errors в
   `scripts/cold_graph_check.py` на shared safe diagnostic; re-raise делать
   detached, где exception выходит в public/library boundary.
3. Перевести card/storage read errors в `scripts/cold_provenance.py`.
4. Перевести все audit errors в `scripts/luna_reenrich_audit.py`.
5. Для `scripts/build_release_manifest.py` выбрать один минимальный путь:

   - если producer официальный: подключить safe argument parsing, path
     resolution, secret-scanned atomic publication и `run_release_cli`;
   - если producer устарел и не используется: удалить или превратить в явный
     non-release compatibility wrapper, который не может создать официальный
     release artifact.

6. Не исправлять только три воспроизведённые строки: AST guard должен стать
   зелёным для всего inventory.

## 7. Фаза 3. Opaque type identity

1. Безопасно извлекать `__module__` и `__qualname__` через базовый
   `type.__getattribute__`, не через dynamic attribute hooks.
2. Принимать только exact `str`, ограничивать длину каждого компонента.
3. Хешировать length-delimited module + qualname; raw metadata не возвращать.
4. При unavailable/invalid metadata использовать fixed sentinel material.
5. Проверить классы:

   - одинаковое имя, разные modules;
   - одинаковый module, разные qualnames;
   - hostile metaclass attributes;
   - secret-shaped module/qualname;
   - control characters и значения длиннее лимита.

## 8. Обязательные regression tests

### Rendering matrix

- `__str__`, `__repr__` и `__format__` печатают marker и/или бросают secondary
  exception;
- checkpoint read, cold manifest read и post-run manifest read;
- cold provenance card/storage read;
- accepted loader insert/read failures;
- Luna audit normalized/card/YouTube failures;
- official release manifest invalid-card path.

Для каждого public path проверить stdout, stderr, serialized artifact,
`traceback.format_exception`, `__cause__` и `__context__`.

### Guard matrix

Добавить synthetic fixture source snippets для каждого запрещённого syntax вида
и safe control snippets с `safe_exception_diagnostic/token`. Guard обязан
поймать unsafe snippets и не дать false positive на safe controls.

### Type-tag matrix

- разные modules при одинаковом class name;
- разные qualnames;
- одинаковая type identity даёт одинаковый fingerprint;
- marker отсутствует в diagnostic и serialized token;
- hostile metadata hooks не вызываются.

Все synthetic tokens создавать только внутри test body. Реальные credentials
запрещены.

## 9. Порядок исполнения и проверки

1. Добавить failing R15 reproductions для f-string leak, inventory gap и type-tag
   collision.
2. Реализовать AST guard и checked-in inventory.
3. Исправить все найденные release-bound rendering paths.
4. Закрыть либо безопасно интегрировать `build_release_manifest.py`.
5. Исправить opaque type identity.
6. Запустить R15 negative matrix.
7. Запустить targeted R12/R13/R14/R15, loader, cold provenance/graph, Luna audit,
   manifest, CLI и publication suites.
8. Запустить AST inventory guard отдельно.
9. Запустить расширенный release regression набор.
10. Ruff и compileall только затронутых Python files.
11. Выполнить `git diff --check HEAD --`.
12. Один общий unit/regression suite запускать только после зелёных
    targeted/static checks.
13. Провести отдельный read-only review итогового diff.

**Полный корпус и все карточки не запускать.** Этот прогон остаётся последней
проверкой всего проекта после завершения исправлений и review без P0/P1/P2 либо
выполняется только если последнее изменение нельзя подтвердить targeted/static
проверками.

## 10. Definition of Done

ТЗ выполнено, когда:

- hostile f-string reproduction больше не вызывает rendering hooks;
- AST guard покрывает весь checked-in release inventory;
- inventory completeness test не допускает незарегистрированный producer;
- все 13 найденных raw rendering paths закрыты;
- статус `build_release_manifest.py` однозначен и проверяем;
- opaque type collision устранена без raw metadata publication;
- targeted, expanded, static и общий unit/regression suite зелёные;
- отдельный review не содержит P0/P1/P2;
- cold/live/final release и all-card run не выполнялись;
- release readiness не объявлена.

## 11. Запрещённые упрощения

- расширять regex ещё одним шаблоном вместо AST guard;
- исправлять только `f"{exc}"`, оставляя `.format`, `%`, logger или print paths;
- считать logging безопасным, если artifact scanner потом отфильтрует файл;
- ловить secondary rendering exception и рендерить его;
- публиковать raw module/qualname ради устранения type collision;
- оставить `build_release_manifest.py` в неопределённом полуофициальном статусе;
- подтверждать исправление cold/live/final release или прогоном карточек.

## 12. Результат исполнения

Исполнение завершено 2026-08-25 в основном потоке без субагентов.

Реализовано:

- regex-only проверка заменена AST guard с проверкой positional и keyword
  rendering paths, f-string, `%`, `.format()`, logger/print, `args`, raw type
  metadata и rendered `raise ... from ...`;
- официальный release boundary закреплён checked-in inventory в
  `scripts/release_identity.py`, а completeness test обнаруживает producer по
  shared CLI/publication/diagnostic helpers;
- все обнаруженные raw exception paths в loader, cold graph/provenance и Luna
  audit переведены на `safe_exception_token()`;
- публичные cold read exceptions создаются и выбрасываются вне исходного
  `except`, поэтому не сохраняют hostile `__cause__`/`__context__`;
- `scripts/build_release_manifest.py` закреплён как официальный producer:
  подключены `SafeArgumentParser`, safe path resolution,
  `atomic_write_release_json()` и `run_release_cli()`;
- opaque type fingerprint теперь хеширует bounded, length-delimited
  `__module__` + `__qualname__`, извлечённые через `type.__getattribute__`;
- добавлена R15 negative matrix для hostile rendering, inventory completeness,
  official manifest publication и module/qualname collision/hook safety.

Проверки:

```text
R15 matrix:                         7 passed
Targeted R12-R15/loader/cold/Luna: 179 passed, 1 skipped, 3 subtests passed
Expanded release regression:       317 passed, 2 skipped, 3 subtests passed
Common unit/regression suite:       981 passed, 2 skipped, 42 subtests passed
Ruff (затронутые файлы):            passed
compileall (затронутые файлы):      passed
git diff --check (scope R15):       passed
Final read-only review:             no P0/P1/P2
```

Два предупреждения pytest относятся к upstream deprecation `codecs.open()` в
`pypinyin`. Git дополнительно сообщил о будущей нормализации CRLF/LF в
`scripts/luna_reenrich_audit.py`; whitespace errors отсутствуют.

Cold/live/final release execution и полный прогон корпуса/карточек не
выполнялись. Они остаются отдельным финальным gate после чистого общего review;
release readiness не объявлена.
