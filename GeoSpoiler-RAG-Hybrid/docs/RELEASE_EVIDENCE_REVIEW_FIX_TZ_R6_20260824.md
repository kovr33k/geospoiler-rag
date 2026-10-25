# ТЗ: независимая аттестация static environment в release-evidence R6

Дата: 2026-08-24  
Статус: **ИСПОЛНЕНО — FINAL RELEASE/COLD/LIVE RUN ЗАПРЕЩЁН**

## 1. Основание

ТЗ подготовлено по результатам повторного read-only ревью текущего
незакоммиченного release-evidence diff в ветке
`work/rag-v2-snapshot-20260810` после исполнения R5.

Подтверждён один блокирующий дефект:

1. **P1 — forged static-environment receipt проходит final verifier.**
   `static_environment_receipt_errors()` проверяет package inventory на
   соответствие диапазонам из `pyproject.toml` и на внутреннюю согласованность
   с `pip show` receipts, но не сравнивает заявленные версии с версиями,
   реально установленными в текущем Python environment. Version strings
   команд `python`, `pytest`, `ruff` проверяются только на непустоту.
   В результате receipt с `version="fixture"` и версиями пакетов
   `9999.0.0` принимается без ошибок. Этот false-green уже закреплён в
   test fixture и проходит через тот же validator, который вызывает final
   verifier.

Воспроизведение на текущем коде:

```text
static_environment_receipt_errors(environment_with_fixture_and_9999_versions)
→ []
```

Фактическая среда в момент ревью сообщала, например:
`Python 3.14.4`, `pytest 9.1.1`, `ruff 0.16.4`,
`lightrag-hku 1.5.4`, `Pillow 12.3.0`.

## 2. Цель

Сделать static-environment evidence независимой аттестацией текущей среды, а
не проверкой внутренне согласованного JSON:

```text
producer command output
        ↕ exact comparison
receipt package inventory
        ↕ independent comparison
current importlib.metadata / current Python runtime
        ↕ dependency constraints + exact dependency bytes
final verifier
```

После исправления нельзя получить `passed`, одновременно подменив все связанные
поля receipt на согласованные вымышленные значения.

## 3. Границы

В scope:

- `scripts/release_identity.py`;
- `scripts/build_release_test_evidence.py` только если producer contract нужно
  дополнить или унифицировать;
- `scripts/build_release_verification.py` для подтверждения использования
  общего validator без обходного пути;
- `tests/test_release_evidence_r3.py`;
- `tests/test_release_evidence_contract.py`;
- другие точечные release-evidence tests только при прямой необходимости.

Вне scope:

- production manifest load и любые изменения production RAG state;
- полный прогон всех карточек/corpus;
- full cold extraction;
- live Late-Fusion/provider calls;
- установка, обновление или удаление Python packages;
- credential rotation/revocation;
- commit, push, merge, tag и deploy.

Текущий security blocker сохраняется отдельно. Это ТЗ не объявляет credentials
отозванными и не даёт release permission.

## 4. Phase 0 — подтверждённые API и локальные контракты

Перед реализацией перечитать:

- `scripts/release_identity.py:192-363` — текущая проверка dependency ranges,
  package receipts и exact dependency bytes;
- `scripts/build_release_test_evidence.py:101-245` — producer version-command и
  `pip show` receipts;
- `scripts/build_release_verification.py:564-574` — final-verifier call site;
- `tests/test_release_evidence_contract.py:61-145` и релевантные tests в
  `tests/test_release_evidence_r3.py` — существующие fixtures и negative matrix.

Разрешённые стандартные API:

- `importlib.metadata.version(distribution_name)` для версии distribution,
  установленной именно в текущем Python environment;
- `importlib.metadata.PackageNotFoundError` для fail-closed missing package;
- `platform.python_version()` либо эквивалент на основе `sys.version_info` для
  версии текущего interpreter;
- `packaging.utils.canonicalize_name()` для сравнения distribution names;
- существующие `packaging.version.Version` и `Requirement.specifier` для
  проверки declared constraints.

Официальные ссылки:

- https://docs.python.org/3/library/importlib.metadata.html
- https://docs.python.org/3/library/platform.html
- https://packaging.pypa.io/en/stable/utils.html

Не добавлять стороннюю dependency и не вызывать network/package index.

## 5. Phase 1 — единый snapshot реально установленной среды

### 5.1. Shared helper

Добавить в `scripts/release_identity.py` общий deterministic helper, например
`current_static_environment_snapshot()`, который возвращает:

- exact текущую Python version;
- exact installed distribution version для каждого элемента
  `STATIC_ENVIRONMENT_PACKAGE_NAMES`;
- нормализованные canonical distribution names;
- machine-readable errors для missing/unreadable/invalid metadata.

Инварианты helper:

1. Package set exact, без missing/extra/duplicate.
2. Lookup выполняется в том же interpreter, чей `sys.executable` обязателен в
   receipt argv.
3. Любой `PackageNotFoundError`, пустая или невалидная version делает snapshot
   failed; исключение не проглатывается как пустой inventory.
4. Helper не читает `.env`, не выводит secrets и не меняет environment.
5. Names сравниваются после канонизации, versions сравниваются как exact
   installed strings. Dependency specifier остаётся отдельной дополнительной
   проверкой, но не заменяет exact comparison.

### 5.2. Формат команд

Определить один parser/expected-value helper для команд:

- `python --version` должен подтверждать exact current interpreter version;
- `python -m pytest --version` должен подтверждать exact installed
  distribution version `pytest`;
- `python -m ruff --version` должен подтверждать exact installed distribution
  version `ruff`.

Parser должен быть anchored к полному ожидаемому first line, а не искать номер
как произвольную substring. Если формат официальной команды не совпал с
ожидаемым, receipt считается failed.

## 6. Phase 2 — fail-closed validator

Расширить `static_environment_receipt_errors()` так, чтобы он независимо
получал snapshot Phase 1 и требовал одновременно:

1. `commands` имеет exact set `python`, `pytest`, `ruff`.
2. Для каждой команды exact field contract, `status="passed"`, `exit_code=0`,
   exact argv, `error=null`, непустой version и version, совпадающая с current
   snapshot.
3. `pip_show.packages` имеет exact package set и каждая version exact равна
   current installed distribution version.
4. Каждый per-package receipt имеет exact package, exact argv, observed name,
   observed version, `status="passed"`, `exit_code=0`, `error=null`; observed
   version одновременно равна inventory и current snapshot.
5. `missing_packages=[]`, `version_errors=[]`, top-level `errors=[]`.
6. Текущая version удовлетворяет current dependency constraints.
7. Exact dependency-file receipt и aggregate identity продолжают
   пересчитываться из текущих bytes, как после R5.

Ошибки должны быть стабильными и адресными, например:

```text
environment_command_version_mismatch:python
environment_command_error_not_empty:pytest
environment_installed_version_mismatch:telethon
environment_installed_package_missing:Pillow
```

Нельзя исправлять forged receipt внутри validator. Только reject.

## 7. Phase 3 — regression и integration tests

### 7.1. Убрать ложноположительные fixtures

`_fixture_test_report()` и другие valid fixtures больше не должны использовать
`fixture`/`9999.0.0` как supposedly valid environment. Они должны строиться из
shared current snapshot либо получать явно внедрённый deterministic snapshot
через mock на границе helper.

Тест не должен случайно превращаться в проверку одной и той же подменённой
функции с обеих сторон. Минимум один integration test обязан использовать
реальные current metadata без mock.

### 7.2. Обязательная negative matrix

Добавить tests, которые изменяют все внутренне связанные поля согласованно:

- заменить package inventory и все matching observed versions одного
  range-compatible package на `9999.0.0` → reject;
- заменить версии всех range-compatible packages и все command receipts
  согласованно → reject;
- `python.version="fixture"` при правильном argv/status/exit → reject;
- fake `pytest` и `ruff` versions, согласованные с fake package inventory →
  reject;
- удалить или заполнить `error` у version-command receipt → reject;
- missing installed package через mock `PackageNotFoundError` → reject;
- extra/missing command и extra/missing package → reject;
- dependency version compatible, но не равна installed version → reject;
- полностью честный producer receipt текущей среды → accept;
- final `_test_evidence()`/`build_report()` принимает честный receipt и
  отклоняет forged receipt, а не только unit helper.

### 7.3. Test isolation

- Не менять реально установленные packages.
- Не обращаться в сеть.
- Для mocked metadata подменять только shared snapshot boundary.
- Тесты не должны зависеть от конкретных глобальных version numbers, кроме
  integration assertion, сравнивающего producer receipt с текущим helper.

## 8. Phase 4 — порядок проверок

После правок сначала выполнить только targeted tests:

```powershell
$env:PYTHONUTF8 = "1"
python -m pytest -q -p no:cacheprovider `
  tests/test_release_evidence_r3.py `
  tests/test_release_evidence_contract.py `
  tests/test_release_followup_contract.py `
  --basetemp=<external-temp>
python -m ruff check `
  scripts/release_identity.py `
  scripts/build_release_test_evidence.py `
  scripts/build_release_verification.py `
  tests/test_release_evidence_r3.py `
  tests/test_release_evidence_contract.py
python -m compileall -q scripts tests
git diff --check HEAD --
```

Если targeted проверки зелёные, выполнить один общий static regression suite:

```powershell
$env:PYTHONUTF8 = "1"
python -m pytest -q -p no:cacheprovider --basetemp=<external-temp>
python -m ruff check loader scripts tests
python -m compileall -q loader scripts tests
git diff --check HEAD --
```

После этого провести новое полное read-only ревью текущего release-evidence
diff. Если найден P0/P1/P2, исправлять его targeted-проверками; общий suite
повторять только если новое изменение затрагивает общий контракт.

**Полный прогон всех карточек не является шагом этого ТЗ.** Он остаётся самым
последним отдельным решением всего цикла: только после исправлений, static
regression и независимого ревью с `0 P0 / 0 P1 / 0 P2`, и только если последние
изменения действительно требуют такого доказательства.

## 9. Definition of Done

ТЗ выполнено только если одновременно:

- receipt с `fixture`/`9999.0.0`, согласованный во всех внутренних полях,
  отклоняется;
- honest receipt совпадает с independently observed current environment;
- command versions, package inventory и per-command `pip show` receipts
  образуют одну exact цепочку;
- dependency constraints и exact dependency bytes остаются обязательными;
- final verifier вызывает общий fail-closed validator;
- negative matrix зелёная;
- targeted и общий pytest, Ruff, compileall, diff-check завершены успешно;
- новое read-only ревью показывает `0 P0 / 0 P1 / 0 P2` в этом scope;
- security/credential blocker не объявлен закрытым без внешнего подтверждения;
- production/corpus/cold/live run не выполнялся.

Выполнение ТЗ не даёт автоматически статусы `READY_FOR_FINAL_RUN` или
`READY_FOR_RELEASE`.

## 10. Запрещённые shortcuts

- Не считать range-compatible version доказательством реально установленной
  version.
- Не сравнивать forged inventory только с forged observed versions.
- Не доверять произвольной непустой строке command version.
- Не мокать producer и validator одной и той же forged функцией во всех tests.
- Не ослаблять exact dependency-bytes contract R5.
- Не устанавливать packages ради прохождения fixture.
- Не запускать полный corpus/card run как промежуточную диагностику.

## 11. Результат исполнения — 2026-08-24

R6 исполнено в заявленном scope:

- В `scripts/release_identity.py` добавлен независимый snapshot текущего
  interpreter/distribution environment через `importlib.metadata` и
  `platform.python_version()`; validator теперь сравнивает receipt с этим
  snapshot, а не только с внутренне согласованными полями JSON.
- Для `python`, `pytest`, `ruff`, package inventory и каждого `pip show`
  receipt закреплены exact argv, exact fields, zero exit, `error=null` и
  exact current versions. Final verifier и producer используют общий
  fail-closed validator.
- Добавлены negative/integration tests для согласованной подделки всех версий,
  fake command versions, non-empty/missing fields, extra/missing commands и
  packages, missing distribution metadata и final `_test_evidence()`.
- Targeted pytest: **67 passed, 2 warnings**.
- Общий pytest: **813 passed, 2 skipped, 42 subtests passed, 2 warnings**.
  Один предыдущий запуск получил transient `OSError` при создании трёх
  version subprocess; изолированный producer-тест и диагностический повтор
  общего suite прошли, постоянная ошибка не воспроизвелась.
- Targeted Ruff, `compileall` и `git diff --check HEAD --` завершены успешно;
  diff-check сообщил только существующие предупреждения о CRLF.
- Прямое воспроизведение forged receipt теперь даёт адресные mismatch-ошибки
  для command/package/current metadata вместо `[]`.

Полный прогон карточек/corpus, production manifest load, cold extraction и
live provider calls **не выполнялись**. Security/credential blocker R5 не
закрыт и release permission не выдан. Полный card/corpus run остаётся самым
последним шагом всего цикла — только после исправлений, static/regression
проверок и независимого ревью с `0 P0 / 0 P1 / 0 P2`.
