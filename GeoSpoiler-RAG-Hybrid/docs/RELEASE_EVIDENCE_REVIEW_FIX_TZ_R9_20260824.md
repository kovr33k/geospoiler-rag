# ТЗ R9: закрытие encoding, metadata и smoke-ledger trust boundaries

Дата: 2026-08-24  
Статус: **ИСПОЛНЕНО 2026-08-25 — FINAL RELEASE / COLD / LIVE / ALL-CARD RUN НЕ ВЫПОЛНЯЛИСЬ**

## 1. Основание

Независимое read-only ревью исполнения R8 подтвердило четыре дефекта уровня P1
и один дефект уровня P2.

### P1-1. UTF-16 без BOM остаётся обходом detector

`scripts/release_identity.py::_release_secret_texts()` декодирует UTF-16 без
BOM только если не менее 30% code units имеют NUL в ожидаемой позиции.
Корректный UTF-16 LE/BE файл с длинным кириллическим префиксом и ASCII secret в
конце не проходит эвристику: raw Latin-1 представление не видит разделённый
байтами token, а UTF-16 представление не создаётся.

Подтверждённый результат для валидного UTF-16 LE без BOM:
`status=passed`, `occurrences=0` при наличии синтетического `sk-or-v1-...`.

### P1-2. Secret-bearing filename не входит в scan boundary и может утечь

Security scan и artifact-tree scan анализируют только содержимое файлов.
Secret-shaped filename при чистом содержимом даёт `status=passed`. Если в
содержимом также есть finding, оба scanner-а копируют raw relative path в
`findings`, поэтому secret из имени файла попадает в report.

Это одновременно false-clean для path-only secret и нарушение запрета на
публикацию matched value через metadata.

### P1-3. Smoke producer расширяет непроверенную attempt history

`scripts/late_fusion_activation_smoke.py::run_smoke()` проверяет у существующего
ledger только JSON root/type и отсутствие secret material. Индексы, attempt IDs,
`previous_attempt_sha256`, собственные hashes и цепочка не валидируются до
append.

Поддельный ledger был расширен новым clean attempt; producer вернул
`passed=true`, хотя `_smoke_is_strict()` отклонил итоговый report. Финальный
verifier fail-closed, но сам producer публикует ложный промежуточный verdict и
нарушает R8-инвариант валидной previous-hash chain.

### P1-4. Shared identity error возвращает raw untrusted values

`scripts/release_identity.py::validate_manifest_run_id()` включает actual и
expected run IDs в `ValueError`. Синтетическое secret-shaped значение напрямую
попадает в exception text. Несколько release producer CLI вызывают helper вне
safe top-level exception boundary, поэтому значение может попасть в stderr/log.

### P2-1. Malformed SQLite роняет security scanner без FAILED report

`scripts/security_release_scan.py::scan()` перехватывает только `OSError` вокруг
`_scan_path()`. Файл с SQLite header и повреждённым body вызывает
`sqlite3.DatabaseError`; scanner аварийно завершается вместо возврата
`status=failed`, `unreadable_files=1` и безопасной диагностики.

## 2. Цель

Сделать secret scan независимым от статистической encoding-эвристики, включить
имена файлов и diagnostic metadata в тот же trust boundary, запретить append к
невалидному smoke ledger и гарантировать безопасный durable FAILED для всех
ошибок security/release validation.

## 3. Обязательные инварианты

1. Валидный UTF-16 LE/BE без BOM сканируется независимо от языка и доли NUL.
2. Raw bytes scan всегда выполняется и не заменяется decoded scan.
3. Один физический secret не дублируется между raw/UTF-8/UTF-16
   representations.
4. Relative path, filename, error path и другие scanner metadata проверяются
   тем же detector до помещения в report.
5. Report никогда не содержит raw secret даже при FAILED verdict.
6. Path-only secret блокирует PASSED security/artifact scan.
7. Existing smoke history валидируется полностью до создания нового attempt.
8. Невалидный ledger не переписывается и не расширяется.
9. Ни run ID, ни path, ни exception value не копируются в durable diagnostics,
   stdout или stderr release CLI.
10. Malformed SQLite и любая ошибка parser/decoder дают durable FAILED report.

## 4. Scope

Основной scope:

- `scripts/release_identity.py`;
- `scripts/security_release_scan.py`;
- `scripts/late_fusion_activation_smoke.py`;
- `scripts/smoke_contract.py` либо новый общий smoke-ledger helper;
- `scripts/build_release_verification.py` только для перехода на общий ledger
  validator;
- release producer entrypoints, использующие shared identity validation, только
  в части safe top-level diagnostics;
- `tests/test_release_evidence_r8.py` либо новый
  `tests/test_release_evidence_r9.py`;
- security/smoke/release contract tests.

Вне scope:

- retrieval, enrichment и RAG business logic;
- изменение official smoke cases;
- cold/live/production/final release execution;
- полный прогон корпуса и всех карточек;
- feature flags, модели, credentials и token rotation;
- commit, push, PR и deploy.

## 5. Фаза 1. Exhaustive byte/encoding scan

1. Для bytes всегда выполнять raw ASCII-compatible scan.
2. При чётной длине дополнительно пытаться строго декодировать весь payload как
   UTF-16 LE и UTF-16 BE независимо от NUL ratio и языка текста.
3. BOM остаётся сильным encoding signal, но не единственным способом включить
   UTF-16 scan.
4. UTF-8/UTF-8 BOM анализировать отдельным представлением при строгом decode.
5. Не использовать `errors=ignore` или `errors=replace` для security verdict.
6. Для каждого decoded match вычислять физический byte span либо безопасный
   fingerprint, позволяющий объединить одну находку между representations.
7. Category counts и unique occurrence counts получать из одного набора
   physical findings.
8. Malformed representation не отменяет результаты других representations.

## 6. Фаза 2. Path и metadata boundary

1. До формирования scanner report проверять:

   - normalized relative path;
   - filename;
   - protected/tracked/staged path metadata;
   - error paths;
   - любые metadata strings, которые будут опубликованы.

2. Path-only finding должен:

   - выставить `status=failed`;
   - увеличить отдельный документированный metadata/path count;
   - не раскрыть matched value.

3. Для safe diagnostics использовать одно из:

   - sanitized relative path с `[REDACTED]`;
   - `path_sha256` плюс безопасный порядковый file ID;
   - оба поля, если это нужно для локального расследования.

4. Raw path хранить только во внутренней переменной процесса. Не помещать его в
   JSON, Markdown, stdout, exception text или failure artifact.
5. `scan_release_artifact_tree()` и `security_release_scan.scan()` должны иметь
   одинаковую metadata semantics.
6. До atomic write просканировать canonical bytes самого security report.

## 7. Фаза 3. Общий smoke-ledger validator

1. Вынести проверку attempt chain в общий helper, используемый producer и final
   verifier. Не импортировать verifier из producer.
2. До append проверить весь существующий ledger:

   - exact root/list shape;
   - strict JSON без duplicate keys;
   - `attempt_index == 1..N` без bool-as-int;
   - уникальный `attempt_id` формата `attempt-NNN`;
   - exact previous-hash chain;
   - каждый `attempt_sha256` равен canonical payload без собственного hash;
   - timezone-aware ordered timestamps;
   - строгие типы `passed`, `case_count`, `cases_sha256`, `errors`;
   - отсутствие unknown security-critical fields;
   - отсутствие secret material.

3. При любом нарушении:

   - не запускать RAG/smoke queries;
   - не append и не переписывать исходный ledger;
   - вернуть/записать отдельный sanitized FAILED diagnostic, если контракт
     предусматривает failure artifact;
   - завершиться non-zero.

4. После append повторно валидировать полную цепочку до atomic write.
5. Final verifier должен использовать тот же helper и дополнительно сверять
   ledger с stamped `attempts`.

## 8. Фаза 4. Safe identity и producer errors

1. `validate_manifest_run_id()` не должен включать actual/expected values в
   exception text. Допустимы только stable code и safe type/presence facts.
2. Проверить все release producer entrypoints, вызывающие `build_identity()`:
   исключение должно проходить через `safe_exception_diagnostic()` до
   публикации или вывода.
3. Path/value-bearing lower-level exceptions не копировать через `str(exc)` и
   `raise ... from exc`, если traceback/CLI может опубликовать cause.
4. Сохранять exception type, component, stable code и message hash без raw text.
5. Негативный exit code и fail-closed control flow сохраняются.

## 9. Фаза 5. Durable malformed-file handling

1. Security scanner должен перехватывать ожидаемые file-analysis ошибки,
   включая минимум:

   - `OSError`;
   - `UnicodeError`;
   - `sqlite3.Error`;
   - безопасно классифицированные detector/parser errors.

2. Ошибка одного файла не должна скрывать остальные результаты scan.
3. Для проблемного файла записывать только sanitized path/file ID и exception
   type/stable code.
4. Итоговые `unreadable_files`, `errors` и `status` должны согласовываться.
5. CLI обязан атомарно записать FAILED report и вернуть non-zero.

## 10. Обязательные regression tests

### Encoding

- UTF-16 LE без BOM: длинный кириллический prefix плюс token обнаруживается;
- UTF-16 BE без BOM: тот же сценарий обнаруживается;
- длинный CJK/Arabic/non-ASCII prefix не создаёт false-clean;
- raw ASCII secret в UTF-16-похожем binary остаётся обнаруженным;
- один physical finding не считается дважды между representations;
- malformed UTF-16 не отменяет raw-byte finding.

### Path metadata

- secret только в filename блокирует PASSED;
- secret в filename и body не появляется в report ни в одном виде;
- secret-bearing unreadable/error path не появляется в diagnostics;
- tracked/staged protected path metadata санитизируется;
- clean paths сохраняют прежний диагностический формат либо документированно
  переходят на safe ID/hash.

### Smoke ledger

- неверный первый index отклоняется до `create_rag()`;
- forged `attempt_sha256` отклоняется;
- broken `previous_attempt_sha256` отклоняется;
- duplicate/invalid attempt ID отклоняется;
- duplicate JSON key отклоняется;
- malformed timestamps/types/unknown critical fields отклоняются;
- invalid ledger остаётся byte-for-byte неизменным;
- валидная multi-attempt chain расширяется и повторно проходит shared validator;
- producer и verifier дают одинаковый verdict для одной chain.

### Safe errors и malformed SQLite

- secret-shaped actual/expected run ID отсутствует в exception, stdout, stderr
  и artifacts;
- каждый release producer identity mismatch завершается non-zero безопасно;
- malformed SQLite создаёт FAILED security report, а не uncaught exception;
- scanner продолжает проверять следующие файлы после malformed SQLite;
- diagnostic не содержит raw filename/path/body fragments.

Использовать только синтетические значения, собранные внутри test body. Реальные
credentials запрещены.

## 11. Порядок проверки исполнения

1. Новая R9 negative matrix.
2. Targeted release-identity/security/smoke suites.
3. Расширенный release/security regression набор.
4. Ruff только затронутых Python-файлов.
5. `compileall` только затронутых Python-файлов.
6. `git diff --check HEAD --`.
7. Один общий unit/regression suite только после полностью зелёных targeted и
   static checks.
8. Отдельное независимое read-only ревью.

**Полный прогон корпуса/всех карточек запрещён.** Он остаётся последним шагом
всего цикла после всех исправлений, регрессионных проверок и независимого ревью
без P0/P1/P2 либо выполняется только если последнее исправление невозможно
подтвердить иначе.

## 12. Definition of Done

ТЗ считается выполненным, когда:

- все четыре P1 и один P2 закрыты кодом и negative regression tests;
- valid UTF-16 LE/BE без BOM не зависит от языка/NUL ratio;
- path-only secret блокирует scan и никогда не публикуется;
- producer не расширяет невалидный smoke ledger;
- producer/verifier используют один chain validator;
- identity mismatch не раскрывает actual/expected values;
- malformed SQLite создаёт durable FAILED report;
- targeted/static/general tests зелёные;
- независимое ревью не содержит P0/P1/P2;
- cold/live/final release и all-card run не выполнялись;
- release readiness не объявлена до отдельного финального gate.

## 13. Запрещённые упрощения

- снизить NUL threshold вместо exhaustive UTF-16 LE/BE scan;
- использовать `errors=ignore`/`replace` для security verdict;
- сканировать только file content, но не path metadata;
- возвращать raw path «для диагностики» при FAILED status;
- доверять ledger только потому, что он strict JSON и не содержит secrets;
- дублировать chain validator в producer и verifier;
- ловить только конкретный тестовый `DatabaseError`, оставляя другие
  `sqlite3.Error` необработанными;
- исправлять дефекты повторным прогоном карточек вместо изменения trust
  boundary.

## 14. Результат исполнения

Все четыре P1 и один P2 закрыты в основном потоке без субагентов:

- byte detector всегда проверяет raw bytes, strict UTF-8 и при чётной длине
  strict UTF-16 LE/BE; совпадения объединяются по физическим byte spans;
- artifact/security scanners блокируют secret-bearing path metadata, публикуют
  только sanitized path и ведут отдельный `metadata_occurrences`;
- malformed SQLite и другие ожидаемые ошибки анализа одного файла превращаются
  в безопасный durable FAILED report, scanner продолжает остальные файлы;
- producer и verifier используют общий exact smoke-attempt-chain validator;
  невалидный ledger отклоняется до `create_rag()` и остаётся неизменным;
- manifest run-id mismatch возвращает только stable
  `manifest_run_id_mismatch`, без actual/expected values.

Проверки окончательного кода:

- R9 negative matrix: `26 passed`;
- targeted R8/R9/security/follow-up: `62 passed`;
- expanded release/security/identity: `185 passed`, `3 subtests passed`;
- full unit/regression suite: `875 passed`, `2 skipped`,
  `42 subtests passed`;
- Ruff: passed;
- compileall: passed;
- `git diff --check HEAD --`: passed, только существующие CRLF informational
  warnings для несвязанных файлов;
- отдельный read-only review в основном потоке: P0/P1/P2 не найдено.

Windows-примечание: environment-receipt test под pytest stdout capture дважды
получил `WinError 50`; те же subprocess-команды напрямую и весь набор с `-s`
прошли. Production code под этот PTY-specific эффект не изменялся.

Полный корпус, карточки, cold/live/final release, commit, push и deploy не
выполнялись. Release readiness этим ТЗ не объявляется.
