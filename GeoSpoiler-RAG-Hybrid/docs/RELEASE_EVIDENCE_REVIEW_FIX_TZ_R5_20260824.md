# ТЗ: устранение integration false-green/false-red в release-evidence R5

Дата: 2026-08-24
Статус: **К ИСПОЛНЕНИЮ — FINAL RELEASE/COLD/LIVE RUN ЗАПРЕЩЁН**

## 1. Основание

ТЗ подготовлено по результатам read-only ревью текущего незакоммиченного
release-evidence diff в checkout `work/rag-v2-snapshot-20260810`.

Подтверждены следующие дефекты:

1. **P1 — static-environment false-green.** Валидатор проверяет только форму
   `dependency_files` и `dependency_spec_identity_sha256`, но не сравнивает их
   с текущими exact bytes `pyproject.toml` и `requirements.txt`. Подмена обоих
   SHA, размеров и aggregate identity на синтетические значения принимается
   без ошибок. Отдельные `pip show` receipts также не содержат observed version,
   поэтому не связаны с `pip_show.packages`.
2. **P1 — accepted-load false-success.** Если после успешной загрузки не удаётся
   построить или проштамповать release identity, machine report переводится в
   `failed`, однако `load_from_manifest()` всё равно возвращает успешное число
   загруженных документов.
3. **P1 — cold producer несовместим с собственным bundle/verifier contract.**
   Успешный `_check()` не выдаёт обязательное поле `errors`, не выдаёт
   `workspace`, тогда как verifier требует `workspace`; при этом report envelope
   вообще не разрешает `workspace`. Реальный успешный cold report поэтому не
   может одновременно пройти producer envelope и final verifier.
4. **P2 — cold report можно перепривязать к post-run manifest bytes.** Cold run
   работает по manifest, прочитанному в начале, но в конце повторно читает путь
   для `manifest_sha256` и заново строит identity. Изменение bytes во время run
   может привязать результат к artifact, по которому операция не стартовала.

Точечная regression-проверка существующего кода завершилась успешно:
`52 passed, 3 subtests passed`. Это не закрывает перечисленные integration gaps.

## 2. Границы

В scope:

- `scripts/release_identity.py`;
- `scripts/build_release_test_evidence.py`;
- `loader/ingest.py`;
- `scripts/cold_graph_check.py`;
- `scripts/build_release_verification.py`;
- точечные и integration regression tests;
- актуализация release-evidence документации после реализации.

Вне scope:

- production manifest load и изменение production RAG state;
- полный прогон всех карточек/corpus;
- cold extraction на полном корпусе;
- live Late-Fusion/provider calls;
- credential rotation/revocation;
- commit, push, merge, tag и deploy.

Существующий security blocker сохраняется отдельно: до подтверждённой
rotation/revocation и свежего zero-finding security report release запрещён.

## 3. Инварианты

1. `passed` environment receipt доказывает exact dependency-spec bytes и
   внутреннюю согласованность каждого command receipt.
2. Успешный return accepted-loader невозможен при `failed` или не записанном
   accepted-load report.
3. Один cold report должен проходить одну общую схему producer → envelope →
   bundle → verifier без ручного дополнения полей.
4. Cold evidence привязывается к одному immutable manifest byte snapshot от
   preflight до stamping.
5. Ни один regression test не использует production paths, реальные secrets
   или внешний provider.

## 4. Phase 1 — exact static-environment receipt

### 4.1. Общий deterministic API

Вынести построение exact dependency-spec receipt в общий helper, доступный и
producer, и validator. Helper должен для фиксированного набора
`pyproject.toml`, `requirements.txt` возвращать:

- exact relative path;
- byte length;
- SHA-256 exact bytes;
- aggregate SHA-256 с однозначным framing path + bytes.

`build_release_test_evidence` использует этот helper при создании receipt.
`static_environment_receipt_errors()` независимо пересчитывает receipt из
текущего checkout и требует полного equality, а не только валидного формата SHA.

### 4.2. Связь package inventory с командами

Каждый `pip show` command receipt должен включать нормализованное observed name
и observed version. Validator обязан проверить:

- exact package set без duplicates;
- exact argv для соответствующего package;
- `status=passed`, `exit_code=0`;
- observed name соответствует package;
- observed version совпадает с `pip_show.packages[package]`;
- `missing_packages=[]`, `version_errors=[]`, `error=null`;
- declared version удовлетворяет текущему dependency spec.

Нельзя считать доказательством отдельный произвольный version string без связи
с command receipt.

### 4.3. Tests

Добавить negative tests:

- заменить оба file SHA на `0*64` при корректной форме → reject;
- изменить только byte count → reject;
- изменить aggregate identity → reject;
- переставить path/bytes framing так, чтобы возникла другая identity → reject;
- изменить `pip_show.packages`, не меняя command observed version → reject;
- duplicate/missing/extra package receipt → reject;
- изменить dependency file после receipt → verifier false.

## 5. Phase 2 — fail-closed accepted-load completion

Изменить `_write_accepted_manifest_report()` так, чтобы результат записи был
наблюдаем вызывающим кодом: вернуть финальный payload либо поднять typed
exception.

Для пути `status="accepted"` действуют правила:

1. Ошибка validation, `build_identity`, `stamp_report` или atomic write сначала
   по возможности публикует sanitized `failed` diagnostic report.
2. После этого `load_from_manifest()` обязательно завершает команду non-zero
   через `RuntimeError`; успешный inserted count не возвращается.
3. Raw exception details и secrets не попадают в report; сохраняются type и
   безопасное сообщение.
4. Failure-report path для уже неуспешной загрузки не должен маскировать
   исходную load exception. Если не удалось записать и diagnostic report,
   исходная exception остаётся primary, а write error добавляется через chaining.

Tests:

- successful LightRAG insert + `build_identity` exception → caller получает
  exception, report `failed`;
- successful insert + `stamp_report` exception → то же;
- successful insert + atomic report write exception → non-zero;
- обычный successful path возвращает exact count и stamped `accepted` report;
- partial/failed insert сохраняет существующий исходный error contract.

## 6. Phase 3 — единый cold report contract

Согласовать `REPORT_CONTRACTS["cold_graph"]`, `_check()`, `_failed_report()` и
`_cold_provenance_is_strict()` через один набор полей.

Минимально обязательные изменения:

1. Successful `_check()` явно выдаёт `workspace` и `errors: []`.
2. `workspace` добавляется в required/allowed payload и получает строгий тип
   `str`.
3. `candidate_path` обязан равняться exact
   `<workspace>/rag_storage` после resolve; verifier не выводит доверенный root
   из непроверенного candidate.
4. Success и failure schemas должны быть раздельно валидируемыми; failure-only
   поля не должны делать successful schema двусмысленной.
5. `stamp_report()` не должен исправлять или достраивать отсутствующие
   producer payload fields, кроме общего envelope.

Добавить сквозной temporary-fixture test:

```text
_check tiny manifest
→ stamp_report
→ validate_report_envelope(kind="cold_graph")
→ build bundle self-preflight
→ _cold_graph_is_strict / final build_report
```

Тест обязан использовать именно payload реального `_check()`, а не вручную
собранный словарь, чтобы schema drift больше не оставался незамеченным.

Negative matrix:

- удалить `workspace` или `errors` → reject;
- workspace/candidate mismatch → reject;
- добавить неизвестное поле → reject;
- failure payload не может быть принят как successful cold evidence.

## 7. Phase 4 — immutable manifest snapshot для cold run

На входе release cold run один раз получить `ManifestValidation` и сохранить:

- validated payload;
- exact `manifest_bytes`;
- exact `manifest_sha256`;
- source snapshots/proof.

Далее `_check()` и stamping используют этот объект, а не повторные независимые
чтения manifest path.

Перед публикацией successful report выполнить final exact-byte comparison с
путём. Если bytes изменились, report становится `failed` с sanitized
`manifest_changed_during_cold_run`; identity к новым bytes не строится.
`build_identity()` получает исходный `manifest_bytes` и уже существующим exact
comparison подтверждает неизменность пути.

Tests:

- whitespace-only изменение manifest во время mocked cold run → failed;
- semantic изменение source entry → failed;
- неизменный manifest → report SHA и release identity равны исходному snapshot;
- resume требует тот же exact manifest SHA, а не только тот же source set.

## 8. Порядок проверок

После каждой phase запускать только соответствующие targeted tests на маленьких
temporary fixtures. После объединения всех исправлений один раз выполнить:

```powershell
$env:PYTHONUTF8 = "1"
python -m pytest -q -p no:cacheprovider --basetemp=<external-temp>
python -m ruff check loader scripts tests
python -m compileall -q loader scripts tests
git diff --check HEAD --
```

Затем провести новое полное read-only ревью текущего diff. Если найден хотя бы
один P0/P1/P2, исправить его и повторить только необходимые targeted/static
проверки; общий suite повторять лишь когда изменение затронуло общий контракт.

Полный прогон всех карточек не является шагом этого ТЗ. Он остаётся самым
последним отдельным решением всего цикла: только когда дальнейшее ревью не
находит P0/P1/P2 и исправлять больше нечего, либо последний набор изменений
действительно невозможно подтвердить иначе.

## 9. Definition of Done

ТЗ выполнено только если одновременно:

- forged dependency/package receipt блокируется producer и verifier;
- accepted-loader не возвращает success при failed/missing identity-bound report;
- реальный tiny cold report проходит весь integration pipeline без fixture-only
  полей;
- любое изменение manifest bytes во время cold run блокирует success;
- targeted negative matrix зелёная;
- общий pytest, Ruff, compileall и diff-check завершены без ошибок;
- новое read-only ревью показывает `0 P0 / 0 P1 / 0 P2` в этом scope;
- security/credential blocker не объявлен закрытым без внешнего подтверждения;
- production/corpus/cold/live run не выполнялся.

Выполнение этого ТЗ не даёт автоматически статусы `READY_FOR_FINAL_RUN` или
`READY_FOR_RELEASE`.

## 10. Запрещённые shortcuts

- Не проверять dependency SHA только regex-формой.
- Не доверять `pip_show.packages` без связи с per-command observed version.
- Не оставлять failed report при process exit code `0`.
- Не чинить cold schema ручным добавлением полей только в test fixture.
- Не строить successful cold identity по повторно прочитанному post-run manifest.
- Не запускать полный corpus/card run как промежуточную диагностику.

## 11. Результат реализации 2026-08-24

- Phase 1 реализована: dependency receipt строится по exact bytes с общим
  framing; `pip show` receipts содержат observed name/version и проверяются на
  согласованность с package inventory.
- Phase 2 реализована: accepted load завершается ошибкой при невозможности
  identity-bound report; diagnostic report sanitized и не маскирует исходную
  load exception.
- Phase 3 реализована: cold payload содержит `workspace` и `errors`, а общий
  cold producer → envelope → verifier contract покрыт round-trip тестом.
- Phase 4 реализована: cold run использует initial manifest byte snapshot,
  checkpoint привязан к его SHA, изменение bytes во время run блокирует success.
- Проверки: `801 passed, 2 skipped, 42 subtests passed`; Ruff, compileall и
  `git diff --check` завершились с exit code `0`.
- Security scan остаётся FAILED из-за текущего физического `.env`: `3`
  protected occurrences. Значения не записывались в report и должны быть
  отозваны/заменены владельцем.
- Полный corpus/card run, production load, cold extraction и live provider
  calls не выполнялись. Они остаются отдельным самым последним шагом после
  независимого ревью без P0/P1/P2 и отдельного решения о необходимости.
