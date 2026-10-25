# ТЗ R7: независимая проверка test evidence и защита release-артефактов от секретов

Дата: 2026-08-24  
Статус: **ИСПОЛНЕНО — ОЖИДАЕТ НЕЗАВИСИМОГО РЕВЬЮ; FINAL RELEASE / COLD / LIVE RUN ЗАПРЕЩЁН**

## 1. Основание

Статическое ревью текущего незакоммиченного release-evidence контура поверх
`bbd2f0d` выявило три блокирующих дефекта уровня P1.

### P1-1. Test evidence не привязан к независимой Git-истине

`scripts/build_release_test_evidence.py` корректно вычисляет `base_commit`,
`merge_base`, `head_commit` и команду
`git diff --check <merge-base>..HEAD --`. Однако verifier в
`scripts/build_release_verification.py`:

- берёт ожидаемый `diff_check_argv` из самого проверяемого payload;
- проверяет Git identity только на непустые строки;
- сравнивает command receipt с другим артефактом того же producer;
- не сверяет `cwd` с корнем проекта;
- не сверяет base/merge-base/HEAD с текущими read-only Git-командами и
  `release_identity.git_commit`;
- не валидирует временную последовательность receipt и доверяет согласованным
  между собой timeout/timestamp-полям.

В результате согласованно подделанный набор с `base_ref=HEAD`,
`git diff --check HEAD..HEAD --`, произвольными SHA и пересчитанными hash-полями
проходит `_test_evidence()` со статусом `passed`.

### P1-2. Release failure paths сохраняют raw exception text

`scripts/build_release_verification.py` записывает `str(exc)` в основной
FAILED report и в отдельный failure artifact. Аналогичный шаблон есть в
`scripts/build_release_evidence.py` и других release producer failure reports.

Воспроизведение с синтетическим исключением, содержащим строку формата
`sk-or-v1-...`, показало, что секрет попадает в
`final_release_verification.json`. Одновременно verifier выставляет
`secret_values_in_report: false` и `artifact_scan_zero_findings` на основании
сканирования project tree, а не фактически собранного evidence bundle и
сгенерированного final report.

### P1-3. Risk register принимает неоднозначный JSON и пропускает секреты

`scripts/build_release_risk_register.py` использует обычный `json.loads`,
поэтому duplicate keys принимаются по правилу «последний ключ победил».
Проверка секрета применяется только к одной записи token risk и использует
regex, не обнаруживающий обычное значение `sk-or-v1-...` в произвольном поле.
Остальные записи `risks` не сканируются, хотя полностью копируются в release
report. Verifier повторяет ту же неполную проверку.

Подтверждён сценарий: duplicate `risks`, закрывающий ранее открытый token risk,
плюс raw token в другой risk-записи принимаются producer и сохраняются в
выходном payload.

## 2. Цель

Сделать release verdict независимым от самосогласованных producer-полей и
гарантировать, что ни входные, ни промежуточные, ни финальные release-артефакты,
включая failure paths, не публикуют raw secret values.

## 3. Обязательные инварианты

1. Проверяемый report не задаёт verifier-у собственную ожидаемую Git-истину.
2. Base ref поступает из доверенного execution contract, а его commit,
   merge-base и текущий HEAD вычисляются verifier-ом независимо.
3. Успешный test gate подтверждает точные argv, cwd, timeout, timestamps,
   exit code, log bytes/SHA и текущую release identity.
4. Любой duplicate JSON key в release input или report означает fail-closed.
5. Secret detector проверяет весь canonical payload, а не одну выбранную risk.
6. Ни JSON, ни Markdown, ни stdout, ни runner log, ни failure/recovery artifact
   не содержат raw secret value.
7. Диагностика сохраняет стабильный error code и exception type; raw
   `str(exc)` в release evidence запрещён.
8. Булевы утверждения `secret_values_in_report` и
   `artifact_scan_zero_findings` выводятся только из реального сканирования
   соответствующих байтов, а не задаются константой.

## 4. Scope

Основной scope:

- `scripts/build_release_test_evidence.py`;
- `scripts/build_release_verification.py`;
- `scripts/build_release_evidence.py`;
- `scripts/build_release_risk_register.py`;
- `scripts/release_identity.py`;
- `scripts/security_release_scan.py` и/или общий модуль secret detection;
- release/security contract tests.

В scope также входят другие release producers только в части удаления raw
exception text из публикуемых отчётов. Не менять их бизнес-логику.

Вне scope:

- cold/live/production execution;
- полный прогон корпуса и всех карточек;
- смена моделей, feature flags, credentials или token rotation;
- миграция production storage;
- commit, push, PR и deploy.

## 5. Фаза 0. Зафиксировать доверенные входы

1. Определить один явный trusted base ref для release execution. Для текущего
   контура — `origin/master`, если отдельный утверждённый контракт не задаёт
   другое значение.
2. Не получать trusted base ref из test report, runner receipt или bundle.
3. Verifier не выполняет `fetch` и не обращается к сети. Требуемый ref должен
   существовать локально до начала проверки.
4. Вынести read-only Git snapshot в одну функцию с результатом:

   - normalized project root;
   - resolved base ref commit;
   - `git merge-base <base-ref> HEAD`;
   - current HEAD;
   - exact diff-check argv.

5. Ошибка Git-команды, отсутствующий ref, пустой/невалидный object id или
   расхождение identity дают FAILED, а не fallback.

## 6. Фаза 1. Независимая верификация test evidence

1. Передать trusted base ref verifier-у через обязательный CLI/config contract,
   не через проверяемый payload.
2. В `_test_evidence()` сравнить report и receipts с независимо вычисленным
   snapshot:

   - `base_ref` равен trusted base ref;
   - `base_commit` равен resolved commit этого ref;
   - `merge_base` равен текущему merge-base;
   - `head_commit` равен текущему HEAD;
   - `head_commit` равен `release_identity.git_commit`;
   - diff argv точно равен
     `git diff --check <current-merge-base>..HEAD --`;
   - `cwd` после безопасной нормализации точно равен project root.

3. Валидировать каждый receipt независимо:

   - ISO-8601 timestamps parseable и timezone-aware;
   - `finished_at >= started_at`;
   - `duration_ms` согласован с интервалом в документированном допуске;
   - `timeout_seconds` — положительное finite число и равно trusted runner
     setting;
   - `timed_out` и `interrupted` — строгие bool;
   - successful gate имеет `exit_code == 0`, `timed_out == false`,
     `interrupted == false`;
   - log существует внутри разрешённого bundle namespace и совпадает по bytes
     и SHA-256.

4. Согласованность report/runner receipt остаётся дополнительной проверкой, но
   не заменяет независимую проверку.
5. Не хардкодить длину Git object id без необходимости: использовать результат
   `git rev-parse --verify` и проверять фактическую валидность объекта.

## 7. Фаза 2. Строгий risk-register contract

1. Читать input через `strict_json_loads`.
2. Зафиксировать exact schema корня и каждой risk-записи:

   - обязательные и допустимые поля;
   - строгие типы без bool-as-int;
   - уникальные непустые `id`;
   - ровно одна `RISK-TOKEN-ROTATION-001`;
   - token risk закрывается только `status=closed` и
     `completion_basis=user_attestation`;
   - unknown fields либо явно запрещены, либо документированно перечислены.

3. Применить единый secret detector ко всему canonical input payload и всему
   stamped output до записи.
4. Не копировать в ошибку найденное значение. Возвращать только finding type,
   count и безопасный JSON path/record id.
5. Verifier повторно валидирует exact risk schema и сканирует весь `risks`, а
   не только token risk.
6. Удалить локальный неполный regex; detector должен разделяться с security
   scan или использовать тот же единый набор паттернов.

## 8. Фаза 3. Безопасные failure artifacts

1. Добавить общий helper безопасной диагностики, возвращающий как минимум:

   - стабильный error code;
   - exception type;
   - безопасный phase/component;
   - при необходимости hash исходного сообщения, но не само сообщение.

2. Удалить raw `str(exc)` из всех публикуемых release paths, включая:

   - основной FAILED final verification report;
   - verification failure JSON/Markdown;
   - release bundle failure/recovery artifacts;
   - test runner lifecycle/summary interruption fields;
   - cold/smoke/manifest producer FAILED reports, если они входят в bundle.

3. Failure artifact остаётся durable и fail-closed даже после sanitization.
4. stdout сообщает только status, safe code и путь к artifact.
5. Не подавлять исходный control-flow: exit code остаётся non-zero, а
   `KeyboardInterrupt`/`SystemExit` не превращаются в success.

## 9. Фаза 4. Сканирование фактического bundle

1. До атомарной публикации просканировать все фактические файлы временного
   evidence bundle, включая test logs, risk register и generated manifests.
2. После формирования final verification JSON/Markdown просканировать их
   точные байты до rename в canonical directory.
3. Любая находка или unreadable file блокирует публикацию PASSED verdict.
4. Failure path не должен копировать заражённый input в новый artifact.
5. Поля `artifact_scan_zero_findings` и `secret_values_in_report` формируются
   из результатов этих проверок. Если проверка не состоялась, значение не
   может быть `true`.
6. Scanner не должен печатать matched value; только path/type/count/SHA.

## 10. Обязательные тесты

### Git/test evidence

- честный producer report с текущим trusted base проходит;
- `base_ref=HEAD` при trusted `origin/master` отклоняется;
- `HEAD..HEAD` с согласованно переписанными report/receipt/hash отклоняется;
- поддельные base commit, merge-base или head commit отклоняются;
- stale HEAD после создания evidence отклоняется;
- чужой cwd отклоняется;
- malformed/naive/reversed timestamps отклоняются;
- несогласованные duration/timeout/timed_out/interrupted отклоняются;
- изменённый log при пересчитанном только одном уровне receipt отклоняется.

### Risk register

- duplicate key в корне и nested risk отклоняется;
- duplicate risk id и две token-risk записи отклоняются;
- raw secret в token risk и в любой другой risk отклоняется;
- synthetic `sk-or-v1`, API key, bearer/access-token и sensitive URL query
  варианты обнаруживаются;
- unknown fields и неверные типы отклоняются согласно exact schema;
- корректная user attestation без secret проходит.

### Sanitization и bundle scan

- synthetic live-looking secret в exception отсутствует в JSON, Markdown,
  stdout и failure/recovery artifacts;
- тот же secret в test log, risk input и любом staging report блокирует PASSED;
- detector не включает matched value в exception text;
- ошибка сканера и unreadable file дают FAILED;
- failed publication оставляет полный проверяемый sanitized artifact и
  non-zero exit;
- `secret_values_in_report` нельзя получить `false`/clean без выполненного
  сканирования точных final bytes.

В тестах использовать только синтетические строки, собранные внутри test body.
Реальные credentials запрещены.

## 11. Порядок проверки исполнения

1. Targeted tests новых негативных матриц.
2. Targeted release/security suites.
3. Ruff только затронутых Python-файлов.
4. `compileall` только затронутых Python-файлов.
5. `git diff --check HEAD --`.
6. Один общий regression suite — только после полностью зелёных targeted и
   static checks, если он нужен для закрытия ТЗ.
7. Независимое read-only code review.

**Полный прогон корпуса/всех карточек в этом ТЗ запрещён.** Он остаётся самым
последним шагом всего цикла и выполняется только после всех исправлений,
регрессионных проверок и независимого ревью без P0/P1/P2, либо если последнее
исправление невозможно подтвердить иначе.

## 12. Definition of Done

ТЗ считается выполненным, когда:

- три P1 закрыты кодом и негативными regression tests;
- verifier самостоятельно подтверждает trusted Git/base identity;
- coherent forgery больше не проходит `_test_evidence()`;
- risk register использует strict JSON и full-payload secret scan;
- raw exception text отсутствует во всех release artifacts;
- фактический временный bundle и final report сканируются до публикации;
- targeted/static/regression проверки зелёные;
- финальное независимое ревью не содержит P0/P1/P2;
- никакой cold/live/final release или all-card run не выполнялся;
- существующие release blockers не объявлены закрытыми без отдельного
  подтверждения.

## 13. Запрещённые упрощения

- сравнивать report только с receipt того же producer;
- брать expected argv/base/cwd из проверяемого payload;
- разрешать `HEAD..HEAD` как release diff evidence;
- заменять strict JSON обычным `json.loads`;
- сканировать только token risk;
- задавать security booleans константами;
- сохранять `str(exc)` «для удобства диагностики»;
- лечить найденные дефекты повторными прогонами вместо исправления контракта;
- запускать полный корпус/карточки до завершения всего цикла исправлений.

## 14. Результат исполнения 2026-08-24

Реализовано без субагентов:

- trusted release base зафиксирован как `origin/master`;
- producer и verifier используют общий независимо вычисляемый Git snapshot;
- verifier проверяет base commit, merge-base, HEAD, release identity, exact diff
  argv, project cwd, timeout, timestamps, duration и lifecycle receipts;
- coherent `HEAD..HEAD` forgery отклоняется;
- risk register использует strict JSON, exact V1 schema, уникальные risk IDs и
  full-payload secret scan;
- общий secret detector допускает только явные redaction markers и не возвращает
  matched values;
- raw exception text удалён из release failure paths в scope;
- test logs, reports, manifests, временный bundle и final JSON/Markdown
  сканируются до атомарной публикации;
- secret-bearing output подавляется и превращает gate в FAILED;
- добавлена отдельная негативная матрица
  `tests/test_release_evidence_r7.py`.

Проверки:

- R7 + security targeted: `22 passed`;
- расширенный release/loader/cold targeted: `215 passed, 3 subtests passed`;
- общий unit/regression suite: `825 passed, 2 skipped, 42 subtests passed`;
- Ruff затронутых файлов: passed;
- compileall затронутых файлов: passed;
- `git diff --check HEAD --`: passed, кроме существующих CRLF warnings Git.

Не выполнялись:

- cold/live/production/final release run;
- полный прогон корпуса или всех карточек;
- commit, push, PR или deploy;
- независимое ревью через субагента — по прямому запрету пользователя.

Следующий gate: отдельное read-only ревью текущего diff. До него release readiness
не подтверждена и полный прогон карточек остаётся запрещён.
