# ТЗ R17: полное замыкание release registry и interrupt boundary

Дата: 2026-08-25  
Статус: **ИСПОЛНЕНО — RELEASE READINESS НЕ ОБЪЯВЛЕНА**

## 1. Основание

После исполнения R16 проведён новый read-only review текущего release boundary в
основном потоке, без субагентов. Проверены production registry, все boundary
entrypoints, AST/inventory gates и interrupt paths.

Текущая матрица остаётся зелёной:

```text
R12 + R15 + R16: 60 passed
Ruff review scope: passed
```

При этом независимые synthetic reproductions подтвердили пять дефектов P1 и два
дефекта P2, которые матрица не обнаруживает.

## 2. Findings

### P1-1. Boundary dependency с CLI обходит safe parser и публикует raw argv

`scripts/security_artifact_redaction.py:389-419` использует обычный
`argparse.ArgumentParser`, печатает resolved root и вызывает `main()` напрямую.
Файл включён в `RELEASE_EXCEPTION_BOUNDARY_FILES` только как dependency
`security_release_scan`, но отсутствует в `RELEASE_PRODUCER_REGISTRY` как CLI.
Поэтому registry-driven matrix R12 его не запускает.

Synthetic unknown argument с secret-shaped marker дал:

```text
exit_nonzero=true
marker_in_stderr=true
stderr_is_safe_json=false
```

CLI также пишет JSON artifacts через локальный `_write_json()` без shared secret
scan и может включать raw `root`, `report_dir` или `quarantine_root`.

### P1-2. Final verifier заменяет сохранённый interrupt до publication guard

`scripts/build_release_verification.py:1756-1761` сохраняет исходный interrupt,
но проверки и side effects до publication `try` остаются вне interrupt-preserving
boundary. В частности, `:1853-1854` выбрасывает новый `RuntimeError`, если
canonical run directory уже существует.

Reproduction `build_report -> KeyboardInterrupt` при существующем
`attempt-001` дал:

```text
observed_exception=RuntimeError
expected_exception=KeyboardInterrupt
```

То же относится к ошибкам sanitization, canonical-name checks и созданию parent
directory до publication `try`.

### P1-3. Late-Fusion smoke заменяет interrupt ошибкой post-processing

`scripts/late_fusion_activation_smoke.py:165-174` сохраняет interrupt, но затем
выполняет chain validation, ledger publication, identity stamping и secret scan.
Только ошибка финального `atomic_write_release_json()` подавляется при interrupt
(`:258-262`).

Synthetic `KeyboardInterrupt` с ошибкой report secret scanner дал:

```text
observed_exception=OSError
expected_exception=KeyboardInterrupt
durable_report_exists=false
```

Ошибка chain validator, ledger writer или `stamp_report()` аналогично может
заменить исходный interrupt.

### P1-4. Test-evidence runner превращает interrupt в обычный failed return

`scripts/build_release_test_evidence.py:524-572` ловит любой `BaseException`,
создаёт synthetic exit code `130`, прерывает цикл, публикует receipts и возвращает
обычный failed report. Исходный interrupt object не сохраняется и не
выбрасывается повторно.

Synthetic `subprocess.run -> KeyboardInterrupt` после подмены тяжёлых preflight
helpers дал:

```text
gate_exception=NONE
summary_interrupted=true
```

Это прямо противоречит общему R16 invariant: durable receipt допускается только
как best effort перед повторным выбросом исходного interrupt.

### P1-5. AST gate всё ещё допускает raw `raise ... from ...`

`tests/test_release_evidence_r15.py:28-59` ищет `ast.Raise` только внутри
именованного `except` handler. Требование R16 запрещает любой non-`None` cause во
всём official boundary независимо от расположения raise.

Synthetic source:

```python
def fail():
    raise RuntimeError("public") from acquire_raw_error()
```

дал:

```text
ast_findings=[]
```

Такой raw cause может быть добавлен после handler, через сохранённый alias либо
вообще вне `except`, и gate останется зелёным.

### P2-1. Schema-only discovery проверяет только `release_evidence_v1`

`tests/test_release_evidence_r15.py:62-72` считает schema-only producer только
если source содержит literal `release_evidence_v1` или имя
`RELEASE_EVIDENCE_SCHEMA`. Другие official schemas из registry игнорируются.

Synthetic direct writer `accepted_manifest_v2` дал:

```text
accepted_schema_only_discovered=false
```

Следовательно, direct writer любого другого registered schema может появиться
без registry entry, safe CLI matrix и AST coverage.

### P2-2. Один schema id обозначает два несовместимых artifact contracts

`scripts/luna_reenrich_control.py:279-291` публикует backup source manifest со
`schema_version=backup_restore_closeout_v1`, полями `created_at`, `files` и
`verified`. `scripts/validate_backup_restore.py:168-181` публикует под тем же
schema id verification report с полями `generated_at`, `files_declared`,
`canonical_mapping`, `status` и `mode`.

Registry связывает этот schema только с `scripts.validate_backup_restore`, хотя
actual direct writer также находится в Luna controller. Это мешает построить
полный schema-based inventory и позволяет двум несовместимым payload shapes
выглядеть как один контракт.

## 3. Цель

Замкнуть release boundary так, чтобы любой artifact writer и любой CLI внутри
boundary автоматически попадал в registry, interrupt нельзя было заменить ни
одной последующей ошибкой, а один schema id всегда обозначал ровно один payload
contract.

## 4. Обязательные инварианты

1. Каждый boundary-файл с `if __name__ == "__main__"` зарегистрирован как CLI
   либо имеет явную проверяемую причину быть non-release entrypoint.
2. Каждый release CLI использует `SafeArgumentParser`,
   `safe_resolve_release_path()` и `run_release_cli()`.
3. Unknown/malformed argv и secret-shaped paths не появляются в stdout, stderr,
   traceback или artifacts.
4. После захвата `KeyboardInterrupt`/`CancelledError` никакой subsequent
   `Exception` не может заменить исходный object.
5. Durable failed receipt выполняется best effort; ошибка receipt publication не
   меняет тип и identity исходного interrupt.
6. Test-evidence runner после durable receipts повторно выбрасывает исходный
   interrupt.
7. Любой `ast.Raise` с non-`None` cause запрещён во всём boundary; разрешён только
   literal `from None`.
8. Все official schema literals и direct writers сопоставлены registry.
9. Один schema id соответствует одному payload shape и одному назначению.
10. Registry содержит данные, необходимые CLI matrix; отдельная ручная таблица
    аргументов не является вторым источником истины.

## 5. Scope

Основной scope:

- `scripts/release_identity.py`;
- `scripts/security_artifact_redaction.py`;
- `scripts/build_release_test_evidence.py`;
- `scripts/build_release_verification.py`;
- `scripts/late_fusion_activation_smoke.py`;
- `scripts/luna_reenrich_control.py`;
- `scripts/validate_backup_restore.py`;
- `tests/test_release_evidence_r11.py`;
- `tests/test_release_evidence_r12.py`;
- `tests/test_release_evidence_r15.py`;
- `tests/test_release_evidence_r16.py`;
- новый `tests/test_release_evidence_r17.py`;
- связанные targeted tests и contract docs.

Вне scope:

- retrieval/enrichment/Wiki business logic;
- cold/live/final release execution;
- полный corpus и прогон всех карточек;
- commit, push, PR и deploy.

## 6. Фаза 1. Red tests

До production changes добавить failing R17 matrix:

1. `security_artifact_redaction.py`:
   - unknown secret-shaped argv;
   - invalid choice;
   - secret-shaped `--root`, `--report-dir`, `--quarantine-root`;
   - marker отсутствует во всех observable channels и artifacts.
2. Final verifier:
   - `KeyboardInterrupt`/`CancelledError` плюс existing run directory;
   - sanitizer failure;
   - parent mkdir failure;
   - failure-artifact publication failure;
   - во всех случаях повторно выбрасывается исходный object.
3. Late-Fusion smoke:
   - chain validator failure;
   - ledger scanner/writer failure;
   - identity stamp failure;
   - report scanner/writer failure;
   - исходный interrupt всегда сохраняется.
4. Test-evidence runner:
   - interrupt на каждом из четырёх gates;
   - lifecycle/summary/receipt publication failures;
   - durable artifacts best effort, затем `pytest.raises` исходного interrupt.
5. AST matrix:
   - raw cause внутри handler;
   - raw cause после handler через alias;
   - raw cause вне handler;
   - nested function/lambda-compatible expressions;
   - `from None` как safe control.
6. Inventory matrix:
   - direct writer каждого official schema;
   - boundary file с direct `__main__`;
   - missing registry CLI metadata;
   - duplicate schema id с несовместимыми contracts.

Synthetic markers создавать только в test body. Реальные credentials не
использовать.

## 7. Фаза 2. Registry и safe CLI closure

1. Расширить `ReleaseProducerSpec` данными CLI probe contract: entrypoint role,
   required argument template/placeholders и expected safe boundary.
2. Строить R11/R12 CLI matrix полностью из registry metadata, без отдельного
   filename-to-arguments mapping.
3. Зарегистрировать `security_artifact_redaction.py` как artifact producer и CLI.
4. Перевести его на shared safe parser, safe path resolution, secret-scanned
   atomic publication и `run_release_cli()`.
5. Добавить AST test: каждый boundary `__main__` либо принадлежит registry CLI,
   либо внесён в минимальный checked-in non-release allowlist с пояснением.
6. Raw resolved paths не печатать; использовать scanner-safe representation или
   opaque identity.

## 8. Фаза 3. Interrupt-preserving finalization

1. Ввести один reusable шаблон/helper для pending interrupt:
   - сохранить исходный object;
   - выполнить sanitized receipt finalization внутри единого guarded блока;
   - подавить только secondary `Exception`;
   - вне всех handlers повторно выбросить исходный object.
2. Final verifier должен включить в этот блок sanitization, output validation,
   parent creation, primary publication и failure publication.
3. При существующем canonical run directory не перезаписывать его; best-effort
   diagnostic публиковать только в уникальный sibling failure directory, затем
   повторно выбрасывать interrupt.
4. Late-Fusion smoke должен защищать весь участок от создания diagnostic до
   report publication, включая ledger и identity stamp.
5. Test-evidence runner должен сохранить interrupt object в gate loop, завершить
   best-effort receipts и выбросить object после цикла; обычные subprocess exit
   codes остаются application failures.
6. `BaseException` не преобразовывать в synthetic exit code без последующего
   re-raise.

## 9. Фаза 4. AST и schema inventory closure

1. Проверку raw cause вынести из handler-specific rendering loop:

```python
for node in ast.walk(tree):
    if isinstance(node, ast.Raise) and node.cause is not None:
        assert isinstance(node.cause, ast.Constant) and node.cause.value is None
```

2. Rendering checks оставить отдельным handler-aware pass.
3. Schema discovery строить по множеству всех schemas из registry и
   `REPORT_CONTRACTS`, анализируя AST dict assignments/publication calls, а не
   один string token.
4. Разделить schema collision:
   - Luna backup source manifest получает отдельный schema id, например
     `backup_source_manifest_v1`;
   - verification report сохраняет `backup_restore_closeout_v1`;
   - legacy Luna manifests допускаются только через явный read-only compatibility
     path, если это необходимо для уже созданных artifacts.
5. Registry должен указывать owner каждого schema и запрещать duplicate ownership
   без явно описанной совместимой формы.

## 10. Проверки

Порядок:

1. R17 red matrix.
2. R17 green matrix.
3. R11-R17 targeted release tests.
4. Security artifact redaction tests.
5. Test-evidence interrupt/publication tests.
6. Final verifier и Late-Fusion interrupt matrices.
7. AST/inventory gate отдельно.
8. Ruff, compileall и `git diff --check` по R17 scope.
9. Один общий unit/regression suite только после зелёных targeted/static checks.
10. Финальный read-only review без P0/P1/P2.

**Не запускать полный corpus/card, cold/live/final release execution.** Полный
прогон карточек остаётся последним gate всего проекта после завершения всех
исправлений и чистого итогового review либо выполняется только если последние
изменения невозможно доказать targeted/static тестами.

## 11. Definition of Done

ТЗ выполнено, когда:

- каждый boundary CLI проходит registry-driven safe CLI matrix;
- security artifact tool не раскрывает synthetic argv/path marker;
- final verifier, Late-Fusion smoke и test-evidence runner сохраняют исходный
  interrupt при любой secondary failure;
- global AST gate обнаруживает любой non-`None` raw cause;
- direct writer любого official schema без registry entry ломает inventory test;
- backup source manifest и backup verification report имеют разные schema ids;
- R17, targeted, expanded и общий unit suite зелёные;
- итоговый review не содержит P0/P1/P2;
- полный corpus/card и release execution не выполнялись;
- release readiness не объявлена.

## 12. Запрещённые упрощения

- оставить `security_artifact_redaction.py` dependency без CLI registry entry;
- добавить его filename только в очередной ручной test tuple;
- сохранять interrupt diagnostic, но возвращать `1` вместо re-raise;
- защищать только последний atomic write, оставив post-processing снаружи;
- искать `raise from` только внутри `except`;
- обнаруживать schema-only producer по одному hardcoded schema token;
- сохранить один schema id для двух разных payload shapes;
- подтверждать исправления полным прогоном карточек или release execution.

## 13. Исполнение и evidence

R17 исполнен 2026-08-25 в основном потоке, без субагентов.

Результат:

- CLI probe metadata перенесены в `RELEASE_PRODUCER_REGISTRY`; ручная таблица
  аргументов R12 удалена;
- `security_artifact_redaction.py` и `luna_reenrich_control.py` зарегистрированы
  как official CLI producers и переведены на shared safe boundary;
- публикация security artifacts выполняется через secret-scanned atomic writer;
- final verifier, Late-Fusion smoke и test-evidence runner сохраняют identity
  исходного `KeyboardInterrupt`/`CancelledError` при secondary failures;
- global AST gate проверяет каждый `ast.Raise` во всём boundary;
- schema inventory строится по всем schemas registry и `REPORT_CONTRACTS`;
- Luna source manifest использует `backup_source_manifest_v1`, verification
  report сохраняет `backup_restore_closeout_v1`; старый input schema разрешён
  только для read-only compatibility.

Проверки:

```text
R17 matrix: 49 passed
R11 + R13 + R14 + R15 + R16 + R17: 93 passed
R12 registry-driven subprocess matrix: 44 passed
Expanded release/security/backup matrix: 354 passed, 1 skipped
Common unit/regression suite: 1055 passed, 2 skipped, 42 subtests passed
Post-helper targeted interrupt/AST matrix: 50 passed
Ruff R17 scope: passed
compileall scripts/tests: passed
git diff --check: passed (line-ending warnings only)
Final read-only review: P0=0, P1=0, P2=0
```

Один совмещённый PowerShell diagnostic run получил Windows
`DuplicateHandle: WinError 6` до запуска дочерних CLI. Изолированный R12 запуск
с тем же текущим кодом прошёл `44 passed`; defect кода не воспроизведён.

Полный corpus/card, cold/live/final release execution не запускались. Commit,
push, PR и deploy не выполнялись. Release readiness не объявлена.
