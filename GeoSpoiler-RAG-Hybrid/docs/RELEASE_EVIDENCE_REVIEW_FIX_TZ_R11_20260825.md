# ТЗ R11: безопасный CLI error boundary и физическая идентичность scanner paths

Дата: 2026-08-25  
Статус: **ИСПОЛНЕНО — RELEASE READINESS НЕ ОБЪЯВЛЕНА; FINAL RELEASE / COLD / LIVE / ALL-CARD RUN НЕ ВЫПОЛНЯЛИСЬ**

## 1. Основание

Read-only review исполнения R10 подтвердило один дефект уровня P1 и один
дефект уровня P2. Существующая R10 matrix проходит, но не проверяет полный
форматированный traceback и не моделирует два разных физических root с
одинаковым public display path.

### P1-1. Safe message не исключает утечку через traceback и path resolution

Release/report paths формируют безопасные верхнеуровневые сообщения, но
оставляют raw lower-level exception в публичном traceback:

- `loader/ingest.py` использует `raise ... from fallback_error`,
  `preparation_error` и `write_error` в accepted-report error paths;
- `scripts/build_release_evidence.py` после safe failure artifact выполняет
  bare `raise` исходного `BaseException`;
- `preflight_release_identity()` и `atomic_write_release_json/text()` вызывают
  `Path.resolve()` до безопасной exception boundary;
- несколько producer entry points повторяют `resolve()` до shared guard.

Synthetic `OSError` с secret-shaped marker подтвердил:

```text
accepted_report_chain: marker_in_public_traceback=True
preflight_resolve: marker_in_public_traceback=True
publication_resolve: marker_in_public_traceback=True
```

Это нарушает R10 invariant: raw lower-level exception и untrusted path не
должны попадать в stdout, stderr, artifact или chained traceback.

### P2-1. Scanner объединяет разные физические files по display string

`scripts/security_release_scan.py` использует `relative.casefold()` как ключ
`metadata_by_path` и collision identity. Поэтому два разных физических файла:

```text
<PROJECT_ROOT>/output/<token>/clean.txt
<external OUTPUT_DIR>/<token>/clean.txt
```

оба получают display `output/[REDACTED]/clean.txt`. Подтвержденный результат:

```text
candidate_count=2
finding_files=2
occurrences=2
metadata_occurrences=1
path_id отсутствует у обоих findings
```

Token в report не попадает, и verdict остается FAILED, но global count
занижен, а два физических finding неразличимы.

## 2. Цель

Закрыть все public exception channels release producers безопасной общей
boundary и считать/differentiate scanner metadata по физической identity, а не
по совпавшему display path.

## 3. Обязательные инварианты

1. Raw exception message, raw untrusted path и secret-shaped marker отсутствуют
   в JSON/Markdown, stdout, stderr и `traceback.format_exception()`.
2. Ошибка resolve/read/write/fsync/replace/cleanup возвращает stable code,
   component, exception type и при необходимости message hash, но не raw text.
3. Safe failure artifact не сопровождается повторным выбросом исходного
   исключения в producer CLI.
4. Preflight и publication path проверяются до mutation; rejection оставляет
   existing artifacts byte-for-byte неизменными.
5. Candidate-level metadata occurrence считается для каждого физического
   файла независимо от его display path.
6. Global `metadata_occurrences` дедуплицирует повторные observations одного
   physical file, но не объединяет разные physical files.
7. Findings с одинаковым sanitized display получают разные стабильные safe
   `path_id`; raw absolute path не сериализуется.
8. Tracked, staged и candidate observations одного project file не увеличивают
   global count повторно.

## 4. Scope

Основной scope:

- `scripts/release_identity.py`;
- `loader/ingest.py` только accepted-manifest report/error publication;
- `scripts/build_release_evidence.py`;
- release producer CLI entry points, использующие shared preflight/publication;
- `scripts/security_release_scan.py`;
- `tests/test_release_evidence_r10.py` либо новый R11 test module;
- соответствующие release/security/manifest regression tests.

Вне scope:

- retrieval, enrichment и Wiki business logic;
- feature flags, model/provider configuration и credentials;
- schema redesign release evidence;
- cold/live/final release execution;
- полный прогон корпуса и всех карточек;
- commit, push, PR и deploy.

## 5. Фаза 1. Shared safe path и publication errors

1. Добавить shared helper безопасного path resolution:

   - сначала проверить lexical path на secret material без `resolve()`;
   - `resolve()` выполнить внутри `OSError/RuntimeError/ValueError` boundary;
   - resolved value повторно проверить на secret material;
   - наружу возвращать только stable exception без raw path/message.

2. Перевести `preflight_release_identity()`, `build_identity()`,
   `atomic_write_release_json/text()` и непосредственные producer preflight
   paths на этот helper.
3. Publication helper обязан нормализовать ошибки open/write/flush/fsync,
   replace и cleanup. Cleanup exception не должен маскировать исходный safe
   diagnostic.
4. Не использовать raw `str(exc)` в публичном сообщении. Message hash допустим
   только как hash и не заменяет exception containment.

## 6. Фаза 2. Public CLI exception boundary

1. Для release producer CLI в scope добавить единый fail-closed boundary:

   - catch `Exception`, но не `KeyboardInterrupt`/`SystemExit`;
   - сформировать safe structured diagnostic;
   - вывести только safe compact JSON в stderr;
   - завершиться non-zero без стандартного chained traceback.

2. В `loader/ingest.py` report publication errors поднимать как safe exception
   с подавленным raw context (`from None`) либо гарантированно перехватывать до
   CLI boundary. Проверка выполняется по полному `traceback.format_exception()`,
   а не только по `str(caught.value)`.
3. Accepted-manifest load может сохранять внутреннюю причинность для library
   diagnostics только если `python main.py load --manifest ...` никогда не
   публикует эту cause в stderr.
4. В `build_release_evidence.py` после записи safe failure artifact не делать
   bare `raise`. Поднять safe publication exception или вернуть контролируемый
   non-zero result.
5. Failure artifact write failure также нормализовать; не ловить только
   `OSError`, если shared guard может вернуть safe `ValueError`/domain error.

## 7. Фаза 3. Physical identity для scanner metadata

1. Расширить внутренний `ScanCandidate` безопасной физической identity:

   - resolved physical key хранится только внутри процесса;
   - public `path_id` строится как hash стабильного root-kind/physical key;
   - raw physical key не попадает в report, stdout или exception.

2. Collision map строить как `sanitized display -> set[physical identity]`, а
   не `set[relative.casefold()]`.
3. Global metadata dedup строить по physical identity:

   - candidate observation — по resolved candidate;
   - tracked/staged project path — по безопасно разрешенной identity внутри
     `PROJECT_ROOT`;
   - повтор tracked + staged + candidate одного файла считается один раз;
   - два разных root/files с одинаковым display считаются два раза.

4. Candidate-level `metadata_occurrences` всегда отражает собственный path и не
   зависит от global dedup.
5. Неизвестная/неразрешимая git или candidate identity дает safe fail-closed
   error code и не объединяется произвольно с другим файлом.
6. Порядок roots, git observations и candidates не должен менять counts или
   `path_id`.

## 8. Обязательные regression tests

### Exception containment matrix

- synthetic secret marker в `OSError` из manifest/output `resolve()`;
- marker в atomic write, fsync, replace и fallback write exception;
- marker отсутствует в `str(exc)`, `repr(exc)`, `__cause__`-rendering,
  `traceback.format_exception()`, stdout, stderr и artifacts;
- accepted report success/failure publication paths;
- bundle publication failure создает safe artifact и не re-raise raw cause;
- existing artifact не меняется при preflight/path rejection;
- parameterized producer CLI test подтверждает safe stderr и non-zero exit.

Все synthetic tokens собирать внутри test body; реальные credentials
запрещены.

### Scanner physical identity matrix

- project `output/...` и external `OUTPUT_DIR/...` с одинаковым raw relative
  path: `finding_files=2`, `occurrences=2`, `metadata_occurrences=2`, два разных
  `path_id`;
- тот же physical project file, наблюдаемый как project/artifacts overlap:
  один candidate и одна global metadata occurrence;
- tracked + staged + candidate одного файла: одна global occurrence, но
  candidate finding сохранен;
- два разных raw secret paths с одинаковым sanitized display различимы;
- перестановка roots/candidates не меняет report semantics;
- serialized report не содержит tokens или absolute paths.

## 9. Порядок исполнения и проверки

1. Добавить failing R11 negative tests для обоих findings.
2. Реализовать shared safe path/publication error boundary.
3. Закрыть producer CLI и accepted-load traceback channels.
4. Перевести scanner counters/collisions на physical identity.
5. Запустить R11 negative matrix.
6. Запустить targeted release/security/manifest suites.
7. Запустить расширенный release regression набор.
8. Ruff и compileall только затронутых Python files.
9. Выполнить `git diff --check HEAD --`.
10. Один общий unit/regression suite запускать только после зеленых targeted и
    static checks и только если изменения затрагивают shared runtime boundary.
11. Провести отдельный read-only review окончательного diff.

**Полный корпус и все карточки не запускать.** Такой прогон остается последним
шагом всего проекта после завершения всех исправлений и review без P0/P1/P2
либо выполняется только когда последнее изменение невозможно подтвердить
targeted/static проверками.

## 10. Definition of Done

ТЗ выполнено, когда:

- P1 и P2 закрыты кодом и negative tests;
- полный formatted traceback каждого synthetic error path не содержит marker;
- producer CLI возвращают безопасный non-zero diagnostic без raw traceback;
- scanner правильно считает два разных physical files с одинаковым display;
- duplicate observations одного physical file не увеличивают global count;
- targeted, expanded, static и при необходимости общий unit/regression suite
  зеленые;
- отдельный review не содержит P0/P1/P2;
- cold/live/final release и all-card run не выполнялись;
- release readiness не объявлена.

## 11. Запрещенные упрощения

- проверять только `str(exc)`, игнорируя `__cause__`, `__context__` и formatted
  traceback;
- заменить bare `raise` другим исключением с raw cause;
- сканировать path только после небезопасного `resolve()`;
- скрывать traceback, продолжая печатать raw exception message вручную;
- дедуплицировать physical files по sanitized/display path;
- добавлять одинаковый `path_id` двум разным physical findings;
- удалять external configured root из scan scope;
- подтверждать исправление cold/live/final release или прогоном всех карточек.

## 12. Результат исполнения

Исполнено 2026-08-25 в основном потоке без субагентов.

- добавлены `ReleaseEvidenceError`, safe lexical/resolved path validation,
  нормализация publication failures и общий `run_release_cli()`;
- release producer entry points переведены на safe CLI boundary;
- accepted-manifest report/load errors больше не сохраняют raw cause/context в
  публичном formatted traceback;
- bundle publication после safe failure artifact поднимает detached safe
  exception вместо исходного `BaseException`;
- scanner использует внутреннюю physical identity для global metadata dedup и
  collision `path_id`;
- разные project/external files с одинаковым display считаются отдельно, а
  overlap одного physical file дедуплицируется;
- добавлена `tests/test_release_evidence_r11.py` с traceback, CLI, physical-root,
  overlap и order-invariance regressions.

Проверки:

- исходная R11 negative matrix до исправления: `7 failed`;
- финальная R11 matrix: `20 passed`;
- targeted R10/R11/security/manifest: `61 passed`, `3 subtests passed`;
- расширенный release regression: `308 passed`, `1 skipped`,
  `3 subtests passed`;
- общий unit/regression suite: `910 passed`, `2 skipped`,
  `42 subtests passed`;
- финальная loader/R11 проверка после context hardening: `37 passed`,
  `3 subtests passed`;
- Ruff, compileall и `git diff --check HEAD --`: passed; сохранены только
  существующие предупреждения Git о будущей CRLF/LF normalization.

Итоговый read-only review R11 scope не выявил P0/P1/P2. Полный корпус, все
карточки, cold/live/final release, commit, push и deploy не выполнялись.
Отдельные ранее известные release blockers этим ТЗ не закрывались.
