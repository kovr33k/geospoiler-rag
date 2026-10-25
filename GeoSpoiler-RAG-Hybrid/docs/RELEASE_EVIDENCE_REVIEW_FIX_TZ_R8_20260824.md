# ТЗ R8: единый secret detector и безопасная публикация release-артефактов

Дата: 2026-08-24  
Статус: **ИСПОЛНЕНО — ОЖИДАЕТ НЕЗАВИСИМОГО РЕВЬЮ; FINAL RELEASE / COLD / LIVE / ALL-CARD RUN ЗАПРЕЩЁН**

## 1. Основание

Независимое read-only ревью исполнения R7 подтвердило три блокирующих дефекта
уровня P1.

### P1-1. Security scan использует только часть общего детектора

`scripts/security_release_scan.py::_count_text_findings()` вызывает
`release_secret_findings()`, но учитывает только категории
`openrouter_token` и `bearer_token`. Категории `credential_assignment` и
`sensitive_query_value` отбрасываются. Локальный regex при этом знает только
`access_token`, поэтому строки вида `api_key=<value>`,
`authorization=<value>` и URL с `?api_key=<value>` дают ноль находок, хотя
общий детектор их обнаруживает.

Следствие: security report может получить `status=passed` при наличии
release-blocking secret material в project tree.

### P1-2. Artifact scan пропускает UTF-16 secret material

`scripts/release_identity.py::release_secret_findings()` декодирует bytes как
Latin-1. Для UTF-16 между ASCII-символами остаются NUL-байты, поэтому
`scan_release_artifact_tree()` не обнаруживает даже синтетический
`sk-or-v1-...` в UTF-16-файле и возвращает `status=passed`.

Следствие: временный evidence bundle и final verification directory могут
быть опубликованы как чистые при наличии секрета в поддерживаемом текстовом
формате.

### P1-3. Smoke producer сохраняет raw secret до bundle gate

`scripts/late_fusion_activation_smoke.py::run_smoke()` копирует model answer и
`late_fusion.channel_statuses` в report, затем напрямую записывает JSON.
Санитизация до `atomic_write_json()` отсутствует. Поздний bundle builder может
отклонить такой report, но raw secret уже остаётся на диске в producer output.

Воспроизведение подтвердило запись как `sk-or-v1-...` из answer, так и
`api_key=...` из channel status. Это нарушает инвариант R7 о том, что входные,
промежуточные и финальные release-артефакты не публикуют raw secret values.

## 2. Цель

Сделать один общий механизм обнаружения secret material для project scan,
bundle scan, payload validation и pre-write sanitization; исключить
false-negative на поддерживаемых кодировках и запретить запись заражённого
smoke report даже до сборки bundle.

## 3. Обязательные инварианты

1. Все release/security consumers используют один набор категорий и одну
   семантику совпадений.
2. Consumer не может выборочно проигнорировать категорию общего detector.
3. Совпадение не возвращает и не печатает secret value.
4. Один физический secret, совпавший с несколькими regex, учитывается как одна
   уникальная находка, сохраняя список категорий.
5. Синтетический allowlist применяется только по точному path и точному
   matched span/value; он не отключает категорию целиком.
6. UTF-8, UTF-8 BOM, UTF-16 LE/BE и ASCII secret bytes в binary-файле не могут
   пройти artifact scan как clean.
7. Ошибка чтения или ошибка анализа файла означает fail-closed.
8. Ни smoke report, ни attempt ledger, ни stdout не содержат raw secret.
9. Если sanitization была нужна, smoke gate не может остаться PASSED.

## 4. Scope

Основной scope:

- `scripts/release_identity.py`;
- `scripts/security_release_scan.py`;
- `scripts/late_fusion_activation_smoke.py`;
- `scripts/build_release_evidence.py` и
  `scripts/build_release_verification.py` только для подключения общего
  detector contract, если потребуется;
- `tests/test_security_release_scan.py`;
- `tests/test_release_evidence_r7.py` либо новый `tests/test_release_evidence_r8.py`;
- smoke contract tests.

Вне scope:

- изменение retrieval/RAG-логики и содержимого smoke cases;
- cold/live/production/final release execution;
- полный прогон корпуса или всех карточек;
- feature flags, модели, credentials и token rotation;
- commit, push, PR и deploy.

## 5. Фаза 1. Единая модель secret finding

1. Заменить интерфейс, возвращающий только агрегированные counts, на общий
   безопасный finding engine. Внутренний finding должен содержать минимум:

   - category;
   - start/end span внутри анализируемого представления;
   - безопасный source/encoding label;
   - без matched value.

2. Поверх finding engine оставить совместимые helpers для:

   - counts по категориям;
   - общего unique occurrence count;
   - fail-closed assertion;
   - redaction.

3. Объединять перекрывающиеся spans одного физического значения. Например,
   `?api_key=...` может соответствовать assignment и query pattern, но не
   должно считаться двумя отдельными секретами.
4. Не включать matched value в exception, report, repr или debug output.
5. Явно определить redaction markers (`[REDACTED]` и URL-encoded вариант) как
   clean и покрыть тестами.

## 6. Фаза 2. Исправить security scan

1. Удалить выборочное сложение только `openrouter_token` и `bearer_token`.
2. Project text scan, NUL-bearing scan и binary fallback должны потреблять
   один общий finding engine и учитывать все release-blocking категории:

   - OpenRouter token;
   - bearer token;
   - access-token/API-key/authorization assignment;
   - sensitive query value.

3. Перенести synthetic fixture allowlist поверх общих findings:

   - allowlist привязан к нормализованному relative path;
   - исключается только точное разрешённое совпадение;
   - соседний неразрешённый secret в том же файле остаётся находкой;
   - ни report, ни ошибка не раскрывают allowlisted value.

4. `finding_files`, `occurrences`, protected-file counts и итоговый status
   должны вычисляться из одного результата, без расходящихся локальных regex.

## 7. Фаза 3. Byte- и encoding-aware artifact scan

1. Добавить общий анализ exact file bytes:

   - raw ASCII-compatible patterns применяются непосредственно к bytes;
   - текстовые представления декодируются детерминированно как UTF-8/UTF-8 BOM
     и UTF-16 LE/BE при наличии BOM либо валидного NUL-паттерна;
   - существующая fallback-кодировка применяется только после строгих
     проверок и не заменяет raw-byte scan.

2. Дедуплицировать одну находку, обнаруженную одновременно raw-byte и decoded
   проходом.
3. Для malformed/unsupported binary всё равно выполнить raw ASCII scan. Ошибка
   decoder не должна молча превращать файл в clean.
4. `scan_release_artifact_tree()` сообщает только path, bytes, SHA-256,
   categories, unique count и safe encoding/type labels.
5. Любая ошибка чтения или анализа оставляет `status=failed`.
6. Bundle builder и final verifier должны использовать этот же byte-aware
   scanner без локальной альтернативы.

## 8. Фаза 4. Pre-write защита smoke artifacts

1. До вычисления финальных hashes и до каждого `atomic_write_json()` применить
   recursive sanitization к данным, полученным из RAG/model runtime, включая:

   - answer;
   - references и URL fields;
   - `channel_statuses` и вложенные error/details;
   - attempt errors;
   - любые новые произвольные trace fields, если они публикуются.

2. Если обнаружено хотя бы одно replacement:

   - сохранить только redacted payload;
   - выставить smoke `passed=false`;
   - добавить стабильный safe error code вроде
     `release_secret_material_redacted` и count без значения;
   - пересчитать `cases_sha256`, attempt hash и цепочку ledger уже по
     sanitized payload.

3. Attempt ledger и основной report должны ссылаться на одинаковые sanitized
   cases/hashes.
4. Перед записью выполнить `assert_no_release_secrets()` для exact canonical
   bytes report и ledger.
5. Не сохранять raw payload во временный, recovery или diagnostic artifact.
6. stdout должен содержать только safe status/count/code.

## 9. Обязательные regression tests

### Общий detector и security scan

- `api_key=<synthetic>` обнаруживается обоими scanner interfaces;
- `authorization=<synthetic>` обнаруживается;
- `access_token=<synthetic>` обнаруживается;
- bearer и `sk-or-v1-...` обнаруживаются;
- `?api_key=`, `?token=`, `?signature=` и `&sig=` обнаруживаются;
- redaction markers не считаются секретом;
- overlapping assignment/query match даёт одну unique occurrence;
- exact allowlisted fixture исключается, соседний secret — нет;
- text, protected file, NUL-bearing и binary branches дают одинаковую
  категориальную семантику.

### Encoding/artifact tree

- синтетический token обнаруживается в UTF-8 и UTF-8 BOM;
- обнаруживается в UTF-16 LE/BE с BOM;
- обнаруживается в UTF-16 LE/BE без BOM при валидном NUL-паттерне;
- ASCII token внутри произвольных binary bytes обнаруживается;
- malformed input не вызывает false-clean;
- unreadable file даёт FAILED;
- ни finding, ни exception не содержит synthetic value.

### Smoke publication

- secret в answer не появляется в output JSON и ledger;
- secret в nested `channel_statuses.error/details` не появляется;
- URL query secret в reference не появляется;
- replacement принудительно делает smoke FAILED;
- `cases_sha256`, attempt hash и previous-hash chain валидны после sanitization;
- исключение с synthetic secret не появляется в report, ledger или stdout;
- чистый smoke payload сохраняет прежнее поведение.

В тестах использовать только синтетические значения, собранные внутри test
body. Реальные credentials запрещены.

## 10. Порядок проверки исполнения

1. Новая негативная матрица R8.
2. Targeted security/release/smoke contract suites.
3. Ruff только затронутых Python-файлов.
4. `compileall` только затронутых Python-файлов.
5. `git diff --check HEAD --`.
6. Один общий unit/regression suite только после зелёных targeted/static checks
   и только если он нужен для закрытия ТЗ.
7. Независимое read-only code review.

**Полный прогон корпуса/всех карточек запрещён.** Он остаётся последним шагом
всего цикла после исправлений, регрессионных проверок и независимого ревью без
P0/P1/P2 либо выполняется только если последнее исправление невозможно
подтвердить иначе.

## 11. Definition of Done

ТЗ считается выполненным, когда:

- все три P1 закрыты кодом и негативными regression tests;
- security scan учитывает все категории общего detector;
- UTF-16 и binary ASCII secret material не проходят artifact scan;
- smoke producer никогда не записывает raw secret до bundle gate;
- sanitization переводит затронутый smoke gate в FAILED и сохраняет
  проверяемую hash chain;
- targeted/static проверки зелёные;
- при необходимости один общий regression suite зелёный;
- независимое ревью не содержит P0/P1/P2;
- cold/live/final release и all-card run не выполнялись;
- release readiness не объявлена без отдельной финальной проверки.

## 12. Запрещённые упрощения

- добавить ещё один локальный regex только в security scanner;
- продолжать выборочно складывать категории общего detector;
- считать Latin-1 универсальным byte scanner;
- считать binary/NUL-bearing file автоматически чистым;
- разрешить позднему bundle rejection оправдывать запись raw producer report;
- redacted report оставить `passed=true`;
- включать matched secret или его фрагмент в диагностику;
- лечить дефект повторным запуском карточек вместо исправления контракта.

## 13. Результат исполнения 2026-08-24

Реализовано без субагентов:

- общий secret finding engine возвращает только безопасные spans, category и
  encoding metadata;
- перекрывающиеся assignment/query patterns объединяются в одну физическую
  находку с сохранением списка категорий;
- security scanner использует все категории общего detector и применяет
  synthetic allowlist только к точному совпадению в точном relative path;
- artifact scanner выполняет raw ASCII-compatible проход и отдельный
  encoding-aware проход для UTF-8/BOM и UTF-16 LE/BE, включая UTF-16 без BOM;
- смешанный binary-файл с UTF-16-похожим NUL-префиксом не скрывает отдельный
  ASCII token;
- smoke producer санитизирует весь runtime artifact до построения cases,
  hashes, attempt ledger и report;
- любая sanitization переводит smoke gate в FAILED и оставляет только safe
  error code/count;
- secret-bearing query/finalizer exception записывается и повторно выбрасывается
  только в безопасном виде;
- recursive sanitizer обрабатывает также строковые object keys и fail-closed
  отклоняет коллизии после redaction;
- добавлена отдельная негативная матрица
  `tests/test_release_evidence_r8.py`.

Проверки:

- финальная R8/security/smoke targeted matrix: `52 passed`;
- расширенный release/security regression набор:
  `153 passed, 3 subtests passed`;
- финальный общий unit/regression suite:
  `849 passed, 2 skipped, 42 subtests passed`;
- Ruff затронутых Python-файлов: passed;
- compileall затронутых Python-файлов: passed;
- `git diff --check HEAD --`: passed, кроме существующих CRLF warnings Git;
- свежие исходные reproductions: security assignment/query обнаружены,
  UTF-16 artifact получил FAILED, raw secret отсутствует в smoke report/ledger.

Один первый запуск расширенного набора получил Windows
`OSError: winerror=50` в существующей environment-receipt subprocess-проверке.
Прямые команды, прямой вызов receipt, изолированный тест и полный повтор прошли;
изменения release-кода под невоспроизводимый сбой не вносились.

Не выполнялись:

- cold/live/production/final release run;
- полный прогон корпуса или всех карточек;
- commit, push, PR или deploy;
- независимое ревью через субагента, согласно прямому запрету пользователя.

Следующий gate: отдельное независимое read-only ревью текущего diff. До него
release readiness не подтверждена, а полный прогон карточек остаётся запрещён.
