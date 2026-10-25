# Повторное статическое ревью release-evidence контура — R2

Дата: 2026-08-18  
Статус: **BLOCKED — NOT READY FOR FINAL RUN**

## Область ревью

Проверен текущий незакоммиченный diff после выполнения
`RELEASE_EVIDENCE_REVIEW_FIX_TZ_20260818.md`: общий envelope, bundle builder,
test receipts, cold proof, smoke ledger, freshness gate и атомарная публикация
финального verdict.

Production `output/`, `rag_storage/`, FTS/registry, manifest load, cold
extraction и live Late-Fusion smoke не изменялись и не запускались.

## Scope check

- Intent: закрыть статически все P1/P2 release-evidence дефекты до одного
  финального operational run.
- Delivered: значительная часть envelope/cold/smoke/failure-path реализована,
  но exact schema, trust boundary bundle, test authenticity, current-state
  reconciliation и атомарная публикация остались частичными.
- Итог: **REQUIREMENTS MISSING**. Статический статус
  `READY_FOR_FINAL_RUN` преждевременен.

## Findings

### P1-1. Path traversal и упаковка произвольного локального файла

**Confidence: 10/10.**

`scripts/build_release_evidence.py:126-160` строит путь назначения из
`check["name"]` и без containment/symlink allowlist читает `log_path` из
report. Report payload SHA не является подписью и может быть пересчитан.

Воспроизведение с `name="../../escaped"` и синтетическим приватным файлом:

- builder завершился `ValueError`;
- файл был скопирован за пределы temporary bundle;
- копия осталась после cleanup;
- итоговый output bundle отсутствовал.

Это локальная file-disclosure/path-traversal граница и release blocker.

### P1-2. Test gate принимает поддельные команды

**Confidence: 10/10.**

`scripts/build_release_verification.py:321-329` проверяет только наличие слов
`pytest`, `ruff`, `compileall`, `diff --check` внутри объединённой строки argv.
Receipts с командами `echo pytest`, `echo ruff`, `echo compileall` и
`echo "diff --check"`, exit code 0 и согласованными логами получили
`_test_evidence(...)["status"] == "passed"`.

Verifier не доказывает, что были выполнены фиксированные команды runner-а.

### P1-3. `git diff --check` не является base-aware

**Confidence: 10/10.**

`scripts/build_release_test_evidence.py:117-128` запускает
`git diff --check`. На чистом release commit эта команда проверяет только
пустой working-tree diff и ничего не говорит о diff ветки относительно base.
Требование предыдущего ТЗ о base-aware gate не выполнено.

### P1-4. Bundle допускает незаявленные физические файлы

**Confidence: 10/10.**

`scripts/build_release_verification.py:192-251` проверяет записи
`evidence_manifest.json`, но не сравнивает их с полным рекурсивным inventory
каталога bundle. После добавления `UNLISTED.txt` в корректный fixture bundle
`_evidence_bundle()` вернул пустой список ошибок.

Следствие: bundle не является exact/self-contained inventory; незаявленный
файл, включая чувствительный, может быть опубликован вне hash manifest.

### P1-5. Registry валидирует только envelope, а не exact payload schema

**Confidence: 10/10.**

`scripts/release_identity.py:31-53` хранит только producer и schema version,
хотя ТЗ требовало `required/allowed fields`. В
`scripts/release_identity.py:415-507` неизвестные обычные payload fields не
отклоняются, обязательные поля конкретного kind не проверяются.

Воспроизведение:

- пустой `security` payload с одним `schema_version` и `errors=[]` принят;
- добавленный `invented_payload_field` также принят после пересчёта payload
  SHA.

Это позволяет builder-у принять семантически неполный или расширенный report.

### P1-6. Freshness/state reconciliation остаётся count-based и не связана с producer snapshot

**Confidence: 9/10.**

`scripts/build_release_verification.py:813-935`:

- provenance exactness сравнивает сохранённые в report ID-set, но не повторяет
  `resolve_source_path()` по текущему `doc_metadata_index.sqlite`;
- `state_inventory` не включает metadata index и GraphML;
- FTS/registry проверяются counts, а не точными source/card/reference sets;
- SHA текущих SQLite вычисляется только в момент verdict и не сравнивается с
  SHA producer snapshot.

Замена одной строки при том же count или изменение metadata index после
provenance producer не обязаны блокировать verdict. Acceptance фазы 6
предыдущего ТЗ не выполнен.

### P1-7. Smoke с последней failed attempt принимается как строгий

**Confidence: 10/10.**

`scripts/build_release_verification.py:589-649` проверяет hash-chain и top-level
cases, но не связывает последнюю attempt с top-level `passed/cases/errors`.
Fixture с валидным hash-chain, `attempts[-1].passed=false`, `case_count=0`,
`errors=["boom"]` и top-level успешными 10 cases получил
`_smoke_is_strict(...) == True`.

Также не проверяется точное равенство declared counts фактическим
references/citations/start URLs.

### P1-8. Финальный verdict публикуется неатомарно

**Confidence: 10/10.**

`scripts/build_release_verification.py:1112-1165` сначала переименовывает temp
directory в final, а затем пишет `verification_manifest.json` уже в
опубликованный каталог. Fault injection на второй записи оставил final
directory с JSON/Markdown, но без verification manifest, после чего `main()`
выбросил исключение.

Это нарушает правило «final directory появляется только полностью готовым».

### P2-1. Builder удаляет уже опубликованный attempt при self-preflight failure

**Confidence: 9/10.**

`scripts/build_release_evidence.py:193-208` выполняет self-preflight после
rename и затем рекурсивно удаляет `output_dir`. Ошибка уничтожает доказательство
неудачной попытки, а path-traversal побочные файлы при этом могут остаться.

### P2-2. Test summary имеет неверную семантику и runner не ограничен timeout

**Confidence: 9/10.**

`scripts/build_release_test_evidence.py:112-151`:

- поле `summary.passed` равно числу успешных команд (максимум 4), а не числу
  тестов;
- stdout/stderr пишутся не через durable atomic writer;
- у subprocess нет timeout, поэтому release gate может зависнуть навсегда;
- staging logs перезаписываются при повторном запуске.

## Что подтверждено зелёным

- Targeted release/cold tests: **38 passed, 1 skipped, 3 warnings**.
- Targeted Ruff: **All checks passed**.
- `git diff --check HEAD`: passed, кроме штатных CRLF warnings.
- Три негативных проверки verifier и две fault/path проверки выше воспроизвели
  defects детерминированно во внешних temp-каталогах.

Зелёные happy-path tests не перекрывают перечисленные negative paths.

## Verdict

P0 не найдено. Найдено **8 P1 и 2 P2**. Финальный production/cold/live прогон
сейчас делать нельзя: он создаст evidence на контракте, который способен
принять поддельный test receipt, failed smoke и stale state, а также способен
опубликовать неполный verdict.

Исправления заданы в
`docs/RELEASE_EVIDENCE_STATIC_FIX_TZ_R2_20260818.md`. До его выполнения и
повторного статического ревью статус — **BLOCKED**.
