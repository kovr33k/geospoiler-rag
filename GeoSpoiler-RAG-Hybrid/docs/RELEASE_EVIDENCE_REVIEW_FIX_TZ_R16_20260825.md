# ТЗ R16: fail-closed manifest, interrupt preservation и полный release gate

Дата: 2026-08-25  
Статус: **ИСПОЛНЕНО — RELEASE READINESS НЕ ОБЪЯВЛЕНА**

## 1. Основание

Независимый read-only review исполнения R15 выполнен в основном потоке без
субагентов. Проверены production release boundary, AST/inventory guards,
exception chaining, CLI boundary и interrupt semantics.

Текущие проверки остаются зелёными:

```text
R15 matrix: 7 passed
Ruff R15/release scope: passed
```

Однако adversarial reproductions подтвердили четыре дефекта P1 и один P2,
которые текущая матрица не обнаруживает.

## 2. Findings

### P1-1. Официальный manifest producer успешно публикует невалидный пустой manifest

`scripts/build_release_manifest.py:39-109` при отсутствии `*.enriched.json`
формирует `sources=[]`, возвращает `errors=0` и публикует
`accepted_manifest_v2`. Общий validator в `loader/manifest_contract.py:232-235`
обязан отвергать такой payload.

Воспроизведение дало:

```text
producer_result.errors=0
producer_result.sources=0
validation_errors=["manifest sources must be a non-empty list"]
```

`tests/test_release_evidence_r15.py:208-220` сейчас закрепляет ошибочное поведение
проверкой `result["sources"] == 0` и наличия опубликованного manifest.

### P1-2. KeyboardInterrupt превращается в обычную release failure

R15 требует не преобразовывать реальный `KeyboardInterrupt` в application
failure, но это происходит в трёх official paths:

- `scripts/cold_graph_check.py:1033` ловит `KeyboardInterrupt`, публикует failed
  report и возвращает `1`;
- `scripts/late_fusion_activation_smoke.py:165-269` сохраняет `BaseException`, а
  затем заменяет его detached `RuntimeError`;
- `scripts/build_release_verification.py:1749-1795` ловит `BaseException`,
  формирует обычный failed report и продолжает publication.

Synthetic `KeyboardInterrupt` в final verifier дал:

```text
main_return=1
status=failed
error=verifier_runtime_error:KeyboardInterrupt:<sha256>
```

Существующий `tests/test_release_remediation_contract.py:152-181` прямо ожидает
преобразование interrupt в return code `1`, поэтому противоречит R15 invariant и
тесту `run_release_cli` на сохранение `KeyboardInterrupt`.

### P1-3. Raw exception chaining обходит AST guard и раскрывает hostile cause

`scripts/build_release_evidence.py:558-578` использует четыре public lock errors
с `raise ... from raw_exception`. R15 AST guard сообщает clean, потому что
`tests/test_release_evidence_r15.py:65-66` помечает `raise from` только когда имя
исключения также встречается в expression нового исключения.

Synthetic source:

```python
try:
    work()
except Exception as exc:
    raise RuntimeError("public failure") from exc
```

даёт `guard_findings=[]`.

Hostile `FileExistsError` через live-lock path дал:

```text
calls=["str"]
marker_in_traceback=true
cause_type=HostileExists
context_type=HostileExists
```

То есть public message безопасен, но raw cause остаётся доступен через traceback,
`__cause__` и `__context__`.

### P1-4. Final verifier находится вне safe CLI/path boundary

`scripts/build_release_verification.py:1741` использует обычный
`argparse.ArgumentParser`, а `:1947` вызывает `main()` напрямую, без
`SafeArgumentParser`, `safe_resolve_release_path()` и `run_release_cli()`.

Неизвестный synthetic argument был дословно опубликован argparse в stderr:

```text
exit=2
marker_in_stderr=true
```

Final verifier также печатает raw output path после publication. Значит path или
argv с secret-shaped marker может выйти в CLI output до artifact scanner.
`RELEASE_PRODUCERS` в `tests/test_release_evidence_r12.py:17-29` final verifier не
содержит, поэтому общий CLI negative matrix его не проверяет.

### P2-1. Inventory completeness не обнаруживает schema-only producers и direct dependencies

`tests/test_release_evidence_r15.py:79-87` обнаруживает файлы только по строкам
shared helpers и одному вручную добавленному `build_release_manifest.py`.
Synthetic producer, который напрямую пишет `release_evidence_v1`, не содержит ни
одного такого token и проходит completeness check.

В actual dependency graph отсутствуют как минимум:

- `scripts/smoke_contract.py`, импортируемый final verifier и Late-Fusion smoke;
- `scripts/security_artifact_redaction.py`, импортируемый security release scan.

Оба файла отсутствуют в `RELEASE_EXCEPTION_BOUNDARY_FILES`, хотя являются direct
release dependencies. Новый schema-only producer по-прежнему можно добавить без
AST проверки.

## 3. Цель

Сделать официальный release boundary fail-closed: producer не может успешно
опубликовать payload, который отвергает consumer; interrupt сохраняет исходную
семантику; public exceptions не удерживают raw cause/context; все CLI и direct
dependencies автоматически входят в единый проверяемый registry.

## 4. Обязательные инварианты

1. Official manifest producer не публикует пустой или невалидный manifest.
2. Payload перед publication проходит тот же shared validator, что loader,
   bundle builder и final verifier.
3. При validation error output artifact отсутствует, public error безопасен и
   detached.
4. `KeyboardInterrupt` после best-effort durable failure receipt повторно
   выбрасывается как исходный `KeyboardInterrupt`, а не заменяется return code или
   `RuntimeError`.
5. `asyncio.CancelledError` не превращается в обычную application failure.
6. `run_release_cli()` не ловит interrupt-style `BaseException`.
7. В official release boundary запрещён любой `raise ... from <exception>`;
   разрешён только `raise ... from None` либо raise заранее созданного безопасного
   исключения вне исходного `except`.
8. Public exception имеет `__cause__ is None` и `__context__ is None`.
9. Все official CLIs используют safe argument parsing и safe path resolution.
10. Synthetic argv/path marker отсутствует в stdout, stderr, traceback и artifacts.
11. Один checked-in registry является источником producers, CLI matrix, AST guard
    и direct dependency inventory.
12. Schema-only producer нельзя добавить в `scripts/` или `loader/` без
    регистрации и AST проверки.

## 5. Scope

Основной scope:

- `scripts/build_release_manifest.py`;
- `loader/manifest_contract.py` только если нужен reusable producer validator;
- `scripts/build_release_evidence.py`;
- `scripts/build_release_verification.py`;
- `scripts/cold_graph_check.py`;
- `scripts/late_fusion_activation_smoke.py`;
- `scripts/release_identity.py`;
- `scripts/smoke_contract.py`;
- `scripts/security_artifact_redaction.py`;
- `tests/test_release_evidence_r12.py`;
- `tests/test_release_evidence_r15.py`;
- новый `tests/test_release_evidence_r16.py`;
- связанные targeted tests.

Вне scope:

- retrieval, enrichment и Wiki business logic;
- cold/live/final release execution;
- полный corpus и прогон всех карточек;
- commit, push, PR и deploy.

## 6. Фаза 1. Red tests

До изменения production-кода добавить failing R16 tests:

1. Empty `ENRICHED_DIR`:
   - `build()` бросает безопасную detached ошибку;
   - output manifest отсутствует;
   - producer не возвращает success.
2. Producer self-validation:
   - каждый опубликованный payload принимается `validate_manifest_payload()`;
   - mutation любого обязательного field блокирует publication.
3. Interrupt matrix:
   - cold graph main;
   - Late-Fusion smoke;
   - final verifier;
   - test-evidence runner как safe control.
4. Для interrupt path проверить durable failed receipt, затем
   `pytest.raises(KeyboardInterrupt)`/`CancelledError` с сохранением исходного
   типа.
5. Raw chaining matrix:
   - `raise Error("fixed") from exc`;
   - `raise Error(token) from alias`;
   - nested handler;
   - `raise ... from None` как safe control.
6. Hostile live-lock exception: marker отсутствует в output/traceback/cause/context.
7. CLI matrix для всех registry producers, включая manifest builder и final
   verifier: malformed/unknown argv и secret-shaped path не публикуются.
8. Inventory matrix: schema-only synthetic producer и missing direct import должны
   провалить completeness test.

Все synthetic markers создавать только внутри test body. Реальные credentials не
использовать.

## 7. Фаза 2. Manifest producer fail-closed

1. После построения payload вызвать shared `validate_manifest_payload()` с
   release defaults.
2. При `cards == []` завершаться ошибкой до создания output.
3. Не дублировать consumer contract новым набором ручных проверок.
4. Validation diagnostics преобразовать через shared safe exception helper.
5. Удалить R15-тест, который считает пустую публикацию успехом, и заменить его
   отрицательным тестом.
6. Сохранить secret-scanned atomic publication для валидного non-empty fixture.

## 8. Фаза 3. Interrupt semantics

Для cold graph, Late-Fusion smoke и final verifier применить единый шаблон:

1. Поймать interrupt только там, где необходимо записать durable receipt.
2. Сформировать receipt через `safe_exception_diagnostic()` без rendering hooks.
3. Завершить best-effort atomic publication.
4. Вне handler повторно выбросить сохранённый исходный interrupt object.
5. Ошибка публикации receipt не должна заменить исходный interrupt.
6. Обычные `Exception` по-прежнему превращать в безопасную detached release
   failure согласно контракту конкретного producer.

## 9. Фаза 4. Detached exception closure

1. AST guard должен запрещать любой `ast.Raise` с non-`None` cause в official
   boundary независимо от текста raised expression и имени alias.
2. Исправить все обнаруженные `raise ... from exc` в inventory, включая lock
   acquisition и helpers `release_identity.py`.
3. Если lower-level identity нужна, включать только
   `safe_exception_token()/diagnostic`, затем выбрасывать safe exception вне
   исходного handler.
4. Добавить programmatic assertions для `__cause__`, `__context__` и formatted
   traceback.

## 10. Фаза 5. Safe final verifier CLI

1. Заменить `argparse.ArgumentParser` на `SafeArgumentParser`.
2. Пропустить `input-dir` и `run-dir` через `safe_resolve_release_path()` до
   side effects.
3. Запускать main через `run_release_cli()`; interrupt должен пройти наружу.
4. Не печатать raw argv/path. Печатать scanner-safe payload либо safe path после
   обязательной проверки.
5. Добавить final verifier в общий registry-driven CLI test matrix.

## 11. Фаза 6. Единый release registry

1. Заменить разрозненные tuples в R12/R15 одним checked-in registry в production
   module.
2. Для каждой записи хранить:
   - producer path;
   - CLI component;
   - official schema(s);
   - direct diagnostic/artifact dependencies.
3. Completeness test строить через AST:
   - imports direct dependencies;
   - `if __name__ == "__main__"` для official scripts;
   - schema literals из registry/`REPORT_CONTRACTS`;
   - publication helpers и direct file publication official schemas.
4. Включить `smoke_contract.py` и `security_artifact_redaction.py` либо явно
   доказать, почему конкретный dependency не пересекает exception/artifact
   boundary.
5. Registry и test matrix не должны иметь отдельные вручную синхронизируемые
   списки.

## 12. Проверки

Порядок:

1. R16 red tests.
2. R16 green matrix.
3. Targeted R12-R16, manifest loader/builder, release bundle lock, cold graph,
   Late-Fusion smoke и final verifier tests.
4. AST inventory guard отдельно.
5. Expanded release/security/publication regression suite.
6. Ruff и compileall затронутых Python files.
7. `git diff --check HEAD --` только по scope R16.
8. Один общий unit/regression suite после зелёных targeted/static checks.
9. Отдельный read-only review итогового diff без P0/P1/P2.

**Cold/live/final release и полный прогон корпуса/карточек не запускать.** Полный
прогон остаётся последним gate всего проекта после завершения исправлений и
чистого review либо выполняется только когда последние изменения нельзя доказать
targeted/static тестами.

## 13. Definition of Done

ТЗ выполнено, когда:

- empty corpus не создаёт accepted manifest;
- каждый опубликованный manifest принимается shared validator;
- interrupt paths записывают durable receipt и повторно выбрасывают исходный
  interrupt;
- ни один official boundary file не содержит raw exception cause;
- hostile live-lock marker отсутствует во всех observable channels;
- final verifier проходит общий safe CLI matrix;
- schema-only producer и missing direct dependency блокируются completeness gate;
- R16, targeted, expanded, static и общий suite зелёные;
- отдельный review не содержит P0/P1/P2;
- corpus/card и release execution не выполнялись;
- release readiness не объявлена.

## 14. Запрещённые упрощения

- разрешать пустой manifest, рассчитывая, что downstream verifier его отклонит;
- писать failed receipt и после этого возвращать `1` вместо повторного interrupt;
- заменять `KeyboardInterrupt` на `RuntimeError`;
- проверять `raise from` только когда exception присутствует в message expression;
- расширять очередной ручной test tuple вместо единого registry;
- считать файл зарегистрированным только потому, что он импортирует safe helper;
- подтверждать исправления full corpus/card или release execution.

## 15. Результат исполнения

Исполнение завершено 2026-08-25 в основном потоке, без субагентов.

Реализовано:

- manifest producer отклоняет пустой corpus и каждый non-empty payload проверяет
  через `validate_manifest_payload()` до atomic publication;
- public manifest diagnostics больше не содержат raw card path, normalized path
  или source id;
- cold graph, Late-Fusion smoke и final verifier сохраняют
  `KeyboardInterrupt`/`CancelledError` после best-effort failed receipt;
- lock acquisition больше не удерживает hostile raw cause/context;
- AST gate запрещает любой non-`None` `raise ... from ...` во всём release
  boundary;
- final verifier переведён на `SafeArgumentParser`, safe path resolution и
  `run_release_cli()`;
- production `RELEASE_PRODUCER_REGISTRY` стал источником CLI matrix, AST
  inventory, official schemas и direct dependencies;
- direct imports `smoke_contract.py` и `security_artifact_redaction.py` включены
  в boundary и проверяются автоматически;
- добавлена R16 regression matrix, включая schema-only producer, hostile lock,
  secret-shaped path, interrupts и publication failure.

Проверки:

```text
R11/R12/R15/R16 + related release contracts: 113 passed
Manifest/R15/R16 final targeted: 45 passed, 3 subtests passed
Common unit/regression suite: 999 passed, 2 skipped, 42 subtests passed
Ruff (R16 scope): passed
compileall (R16 scope): passed
git diff --check (tracked R16 scope): passed
Final read-only diff review: no P0/P1/P2
```

Common suite был выполнен один раз. После него итоговая локальная hardening-правка
manifest diagnostics проверена только targeted/static матрицей, чтобы не
повторять дорогой общий прогон без сильной необходимости.

Не выполнялись: полный corpus/card run, cold/live/final release execution,
commit, push, PR и deploy. Финальный прогон всех карточек остаётся отложенным до
конца всего цикла исправлений и чистого итогового review проекта.
