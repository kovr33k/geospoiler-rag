# ТЗ R10: закрытие producer publication, identity preflight и scanner path-root boundaries

Дата: 2026-08-25  
Статус: **ИСПОЛНЕНО — RELEASE READINESS НЕ ОБЪЯВЛЕНА; FINAL RELEASE / COLD / LIVE / ALL-CARD RUN НЕ ВЫПОЛНЯЛИСЬ**

## 1. Основание

Read-only review исполнения R9 подтвердило три дефекта уровня P1 и один дефект
уровня P2. Текущая R9 negative matrix проходит (`26 passed`), но не покрывает
эти integration paths.

Review выполнялся по R9 scope и его непосредственным release integration
points. Накопленный branch diff относительно `origin/master` значительно шире
R9 (`404 files`, `66848 insertions`, `20280 deletions`) и не считается scope
этого ТЗ.

### P1-1. Не все release producers защищают artifact и stdout перед публикацией

Shared `validate_manifest_run_id()` больше не возвращает actual/expected values,
но это закрывает только mismatch exception. При совпадающем secret-shaped run ID
`build_identity()` и `stamp_report()` сохраняют raw значение в:

- `release_identity.release_run_id`;
- `command`;
- `command_argv`;
- producer-specific fields и failure artifacts.

Часть producers вызывает `assert_no_release_secrets()` перед записью, часть
пишет stamped report напрямую. Подтверждено на
`scripts/validate_backup_restore.py`: producer завершился с `return_code=0`, а
synthetic `sk-or-v1-...` оказался одновременно в JSON artifact и stdout.

Затронуты как минимум:

- `scripts/validate_backup_restore.py`;
- `scripts/migrate_portable_paths.py`;
- `scripts/sync_media_contract.py`;
- `scripts/graph_provenance_reconcile.py`;
- `scripts/build_release_test_evidence.py`;
- `scripts/luna_reenrich_audit.py`;
- failed-publication artifact в `scripts/build_release_evidence.py`;
- failed accepted-manifest report в `loader/ingest.py`.

Отдельный путь утечки существует до identity builder: `load_manifest_file()` и
manifest validation включают raw exception/path в `validation.errors`, после
чего producers объединяют их в uncaught `ValueError`. Missing manifest с
secret-bearing filename подтвердил наличие synthetic token в diagnostic text.

### P1-2. Identity preflight выполняется после необратимых или дорогих действий

Некоторые producers проверяют manifest/run binding только после основной
работы.

Подтверждено для `late_fusion_activation_smoke.run_smoke()` при mismatch:

- `create_rag()` вызван один раз;
- выполнены все 10 smoke queries;
- `smoke_attempts.json` записан;
- только после этого получен `manifest_run_id_mismatch`;
- итоговый smoke report не записан.

Аналогичный порядок виден в коде:

- `migrate_portable_paths.main()` вызывает `apply()` до `build_identity()`;
- `validate_backup_restore.main()` запускает validation и пишет report до
  identity binding;
- `build_release_test_evidence.main()` запускает test gate до identity binding;
- read-only scanners/reconcilers выполняют дорогую работу до preflight.

Mismatch или невалидный manifest должен останавливать producer до mutation,
LLM/network calls, ledger append, report overwrite и дорогого test/scan run.

### P1-3. Security scanner падает на разрешенном external configured root

`config.OUTPUT_DIR` может быть абсолютным и находиться вне `PROJECT_ROOT`.
`_candidate_files()` включает такой root, но `_inventory_digest()` без защиты
вызывает:

```python
path.resolve().relative_to(config.PROJECT_ROOT.resolve())
```

Основной scan loop повторяет то же предположение через
`path.relative_to(config.PROJECT_ROOT)`. С clean file в external configured
`OUTPUT_DIR` вызов `scan()` подтвердил uncaught `ValueError` вместо durable
FAILED report. Exception text содержит имя external candidate.

Тот же класс отказа возможен для symlink/reparse target, path resolution error
и ошибки candidate discovery до входа в per-file exception boundary.

### P2-1. Metadata counters зависят от порядка git/candidate обработки

`safe_path()` возвращает `(sanitized_path, 0)`, если raw path уже встречался в
tracked/staged metadata. Поэтому один и тот же protected candidate дает разные
`findings`, `occurrences` и `protected_occurrences` в зависимости от git state.

Подтвержденный path-only сценарий для tracked `.../.env`:

```text
status=failed
metadata_occurrences=1
finding_files=0
occurrences=0
protected_occurrences=0
protected_files=[{"path":"[REDACTED]/.env","occurrences":0}]
```

Global fail-closed verdict сохраняется, но report одновременно утверждает, что
protected file clean, а artifact-tree scanner для того же physical path создает
finding. Это нарушает R9 invariant одинаковой metadata semantics.

## 2. Цель

Сделать identity и secret validation единым preflight boundary для всех release
producers, исключить raw untrusted values из artifacts/stdout/stderr, обеспечить
durable scanner verdict для configured external roots и сделать metadata counts
детерминированными независимо от git inspection order.

## 3. Обязательные инварианты

1. Manifest/run identity валидируется до mutation, LLM/network calls, ledger
   append, test gate, scan и report overwrite.
2. Secret-shaped run ID, manifest path, output path или validation diagnostic не
   попадает в JSON, Markdown, stdout, stderr или failure artifact.
3. Каждый release JSON/Markdown publication проходит общий final secret scan.
4. Failure artifact использует только stable code, component, exception type,
   safe counters и message hash.
5. Raw lower-level exception не возвращается через `str(exc)` или chained
   traceback в producer CLI.
6. Configured external `OUTPUT_DIR` обрабатывается как именованный allowed root,
   а не как path relative to `PROJECT_ROOT`.
7. Неизвестный external/symlink/reparse candidate дает sanitized durable FAILED,
   scanner продолжает остальные допустимые files.
8. Один physical metadata secret учитывается один раз в global
   `metadata_occurrences`, но не исчезает из candidate finding из-за более
   раннего tracked/staged observation.
9. Artifact-tree и security scanner возвращают одинаковую candidate-level
   metadata semantics.
10. Невалидный identity preflight не изменяет existing artifacts byte-for-byte.

## 4. Scope

Основной scope:

- `scripts/release_identity.py`;
- `loader/manifest_contract.py`;
- `scripts/security_release_scan.py`;
- `scripts/late_fusion_activation_smoke.py`;
- `scripts/migrate_portable_paths.py`;
- `scripts/validate_backup_restore.py`;
- `scripts/build_release_test_evidence.py`;
- `scripts/build_release_risk_register.py`;
- `scripts/graph_provenance_reconcile.py`;
- `scripts/sync_media_contract.py`;
- `scripts/luna_reenrich_audit.py`;
- `scripts/build_release_evidence.py`;
- `loader/ingest.py` только в части release report publication;
- `scripts/build_release_verification.py` только если меняется shared envelope;
- release/security/smoke regression tests.

Вне scope:

- retrieval, enrichment и Wiki business logic;
- official smoke questions;
- feature flags, model/provider configuration и credentials;
- cold/live/final release execution;
- полный прогон корпуса и всех карточек;
- commit, push, PR и deploy.

## 5. Фаза 1. Shared release preflight и publication boundary

1. Добавить общий helper, который до основной работы:

   - strict-loads manifest;
   - проверяет manifest contract;
   - проверяет run binding;
   - строит identity из validated bytes;
   - проверяет secret material во всех identity/publication metadata;
   - возвращает только validated manifest snapshot и identity.

2. Secret-shaped run ID не санитизировать в другую identity. Такой run должен
   быть отклонен stable code до выполнения producer body.
3. Manifest validation errors преобразовывать в safe structured diagnostics.
   Raw path, source ID, field value и exception text не переносить.
4. Добавить общий publication helper для release JSON/Markdown:

   - canonical serialization;
   - `assert_no_release_secrets()` непосредственно перед atomic write;
   - безопасное failure artifact поведение;
   - отсутствие raw output path в exception/stdout.

5. Запретить release producers напрямую вызывать `atomic_write_json()`,
   `Path.write_text()` или печатать полный stamped report без final guard.
6. Failed publication artifacts не должны содержать raw `release_run_id`,
   `output_dir`, manifest path или исходный exception message.
7. `payload_sha256` вычислять после окончательной safe representation, чтобы
   sanitization не инвалидировала envelope hash.

## 6. Фаза 2. Identity preflight до side effects

Переставить control flow каждого producer так, чтобы preflight завершался до
producer work.

Обязательные проверки:

- `late_fusion_activation_smoke`: identity и existing ledger валидны до
  `create_rag()`; mismatch дает zero queries, zero finalizer calls, no ledger
  append и no report write;
- `migrate_portable_paths --mode apply`: identity валиден до первого file write;
- `validate_backup_restore`: release identity валиден до создания/перезаписи
  release report;
- `build_release_test_evidence`: identity валиден до `run_gate()`;
- `security_release_scan`, provenance reconcile и audit: identity валиден до
  дорогого scan/audit;
- `sync_media_contract`: сохранить уже существующий pre-apply guard и перевести
  publication на общий helper;
- `build_release_evidence`: preflight/failure path не публикует raw identity.

Runtime failure после успешного preflight по-прежнему обязан оставить safe
FAILED evidence там, где это предусмотрено контрактом.

## 7. Фаза 3. Candidate-root model security scanner

1. Заменить bare `Path` candidate на внутренний descriptor минимум с полями:

   - physical path только для процесса;
   - root kind (`project`, `output`, `artifacts`, `source_registry`);
   - normalized relative display path;
   - stable file ID/path hash для unsafe diagnostics.

2. Relative path вычислять относительно того declared root, из которого
   candidate был получен. Не применять `relative_to(PROJECT_ROOT)` ко всем
   candidates.
3. Deduplicate physical files по resolved identity, сохраняя один стабильный
   root/display mapping.
4. Symlink/reparse target вне declared roots отклонять до чтения с safe code.
5. Обернуть candidate discovery, path resolution и inventory construction в
   fail-closed boundary. Ошибка одного root/file не должна отменять findings по
   остальным files.
6. Inventory digest строить только из safe root label, sanitized relative path,
   size/hash/error code. Raw absolute path не сериализовать.
7. Clean file во внешнем configured `OUTPUT_DIR` должен либо сканироваться как
   `output/<relative>`, либо давать документированный safe FAILED. Uncaught
   exception запрещен.

## 8. Фаза 4. Детерминированные metadata counters

1. Разделить:

   - sanitization/count конкретного observation;
   - dedup global physical metadata occurrences.

2. Повторное появление path в tracked/staged set не должно заставлять
   candidate-level count стать нулем.
3. Candidate finding всегда отражает собственный path occurrence.
4. `protected_occurrences` одинаково считается для tracked и untracked
   protected candidate.
5. `metadata_occurrences` остается deduplicated global count.
6. Если разные raw paths дают одинаковый `[REDACTED]` display, сохранить
   различимость через safe file ID/path hash без публикации raw path.

## 9. Обязательные regression tests

### Producer publication matrix

Параметризованный тест для каждого release producer:

- matching synthetic secret-shaped run ID отклоняется до producer body;
- token отсутствует в artifact bytes, stdout, stderr и exception chain;
- secret-bearing manifest/output path не публикуется;
- manifest validation error с raw path превращается в stable safe diagnostic;
- existing output/failure artifact при preflight rejection не изменяется.

Отдельно проверить `build_release_evidence` failure artifact и
`loader.ingest` failed report.

### Preflight ordering

- Late-Fusion mismatch: `create_rag.await_count == 0`, query count `0`, ledger
  отсутствует или byte-for-byte неизменен;
- invalid existing ledger и invalid identity оба fail before RAG;
- portable-path mismatch: `apply()` не вызван;
- backup mismatch: validation/report writer не вызван;
- test-evidence mismatch: `run_gate()` не вызван;
- read-only producers не начинают expensive scan/audit до preflight.

### Scanner roots и durable failures

- clean external configured `OUTPUT_DIR` не вызывает exception;
- secret body и secret filename во внешнем root блокируют PASSED без leak;
- candidate вне всех declared roots дает durable FAILED;
- symlink/reparse escape дает durable FAILED;
- path resolution/discovery error не скрывает finding следующего файла;
- report/CLI не содержит raw external absolute path;
- inventory digest стабилен при одинаковом root-relative содержимом.

### Metadata semantics

- tracked protected candidate с path-only secret имеет
  `finding_files=1`, `occurrences=1`, `metadata_occurrences=1`,
  `protected_occurrences=1`;
- тот же candidate без tracked state дает те же counts;
- staged + tracked duplicate не увеличивает global metadata count;
- artifact-tree и security scanner согласованы по candidate occurrence;
- collision sanitized display paths различимы безопасным ID.

Использовать только synthetic values, собранные внутри test body. Реальные
credentials запрещены.

## 10. Порядок исполнения

1. Добавить failing R10 negative tests для четырех findings.
2. Реализовать shared preflight и safe publication helper.
3. Перевести producers на preflight-before-side-effects.
4. Ввести candidate-root descriptor и durable discovery errors.
5. Исправить metadata counter semantics.
6. Запустить R10 negative matrix.
7. Запустить targeted release/security/smoke/manifest suites.
8. Запустить расширенный release regression набор.
9. Ruff и compileall только затронутых Python files.
10. `git diff --check HEAD --`.
11. Один общий unit/regression suite только после зеленых targeted/static checks.
12. Отдельный read-only review окончательного diff.

**Полный корпус и все карточки не запускать.** Такой прогон остается последним
шагом всего проекта после всех исправлений и review без P0/P1/P2 либо выполняется
только если последнее изменение невозможно подтвердить иначе.

## 11. Definition of Done

ТЗ выполнено, когда:

- все 3 P1 и 1 P2 закрыты кодом и negative tests;
- ни один release producer не публикует secret-shaped identity/path metadata;
- identity mismatch не вызывает mutation, LLM/network calls или дорогой gate;
- external configured roots не роняют scanner;
- scanner всегда создает safe durable FAILED на path/discovery errors;
- metadata counts не зависят от tracked/staged ordering;
- producer matrix, targeted, expanded, static и общий unit/regression suite
  зеленые;
- отдельный review не содержит P0/P1/P2;
- cold/live/final release и all-card run не выполнялись;
- release readiness не объявлена.

## 12. Запрещенные упрощения

- добавить `assert_no_release_secrets()` только в один подтвержденный producer;
- исправить только mismatch message, оставив matching secret-shaped identity;
- валидировать identity после side effects и затем удалять созданные artifacts;
- ловить `ValueError` external path и молча исключать candidate;
- публиковать raw absolute path ради диагностики;
- считать global `metadata_occurrences` достаточным и оставлять contradictory
  candidate/protected counters;
- менять schema fields без синхронного обновления report contract и verifier;
- подтверждать исправление cold/live/final release или прогоном карточек.

## 13. Результат исполнения

Исполнено 2026-08-25 в основном потоке без субагентов.

- добавлен shared identity preflight и guarded atomic JSON/UTF-8 publication;
- matching secret-shaped run ID отклоняется до producer body;
- smoke, cold graph, portable migration, backup validation, test evidence,
  security scan, provenance reconcile, media sync, risk register и Luna audit
  выполняют release preflight до дорогих или изменяющих состояние действий;
- failure artifacts и manifest diagnostics больше не копируют raw exception/path
  material;
- security scanner поддерживает external configured roots и fail-closed
  discovery/path/reparse errors;
- metadata counters отделены от global dedup, а redacted path collisions получают
  безопасный `path_id`;
- добавлена `tests/test_release_evidence_r10.py` с producer-ordering, publication,
  external-root, reparse, collision и counter regressions.

Проверки:

- R10 matrix: `15 passed`;
- финальные targeted cold/R10 regressions: `40 passed`;
- расширенный release regression: `270 passed`, `3 subtests passed`;
- общий unit/regression suite: `887 passed`, `2 skipped`, `42 subtests passed`;
- Ruff и compileall по затронутым файлам: passed;
- `git diff --check HEAD --`: passed, только существующие предупреждения о
  последующей нормализации CRLF/LF.

Стандартный pytest fd-capture на Windows/Python 3.14 возвращал `WinError 50` для
вложенных version subprocesses. Причина подтверждена повтором: тот же тест и
команды проходят с `--capture=sys`; код receipt не изменялся ради обхода.

Полный корпус, все карточки, cold/live/final release, commit, push и deploy не
выполнялись. Отдельные ранее известные release blockers этим ТЗ не закрывались.
