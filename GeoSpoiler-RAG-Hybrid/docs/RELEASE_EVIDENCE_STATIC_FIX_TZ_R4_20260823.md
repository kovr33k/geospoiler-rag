# ТЗ: закрытие security и manifest-integrity blockers release-evidence R4

Дата: 2026-08-23
Статус: **STATIC/REGRESSION IMPLEMENTED — CREDENTIAL ROTATION REQUIRED; FINAL RELEASE/COLD/LIVE RUN FORBIDDEN**

## 1. Основание и границы

ТЗ подготовлено по результатам read-only ревью текущего checkout
`work/rag-v2-snapshot-20260810` против `origin/master`.

Оно является follow-up к `RELEASE_EVIDENCE_STATIC_FIX_TZ_R3_20260818.md` и
закрывает остаточные findings, подтверждённые текущим ревью:

- **P1 — security false-green:** сканер обнаруживает credential-like material в
  физическом `.env`, но переносит его только в `protected_files`; exit code и
  финальный verifier проверяют другие поля и возвращают зелёный результат.
- **P2 — manifest TOCTOU:** accepted manifest валидирует и хеширует карточку,
  после чего loader повторно читает файл и загружает уже не обязательно те же
  байты.
- **P2 — parser drift:** несколько release producers используют обычный
  `json.loads()` для accepted manifest вместо общего strict loader.
- **P2 — неполное статическое окружение:** текущая среда не собирает весь test
  suite из-за отсутствующего `streamlit`, а `ruff` недоступен.

В scope входят только код, контракты, regression-тесты и статические проверки.

В scope **не входят**:

- production manifest load;
- запись или изменение `output/`, `rag_storage`, FTS, registry и Wiki state;
- cold LightRAG extraction;
- live Late-Fusion smoke и внешние provider calls;
- blind human review и изменение acceptance scores;
- commit, push, merge, tag, deploy или удаление пользовательских артефактов.

До прохождения Definition of Done финальный release run запрещён.

Критическое правило по времени выполнения: **полный прогон всех карточек/N/N
не выполняется ни в одной Phase 0–4**. Ни один разработчик не должен повторно
перезапускать corpus после каждого изменения. Все regression и TOCTOU tests
работают на маленьких temporary fixtures.

## 2. Инварианты решения

1. Один accepted manifest имеет одну трактовку во всех consumers.
2. `protected_files` не является информационным исключением для найденного
   секрета: любое ненулевое количество credential findings в protected-файле
   блокирует security gate.
3. SHA в manifest должен покрывать именно байты, переданные в LightRAG, а не
   предыдущую независимую проверку того же пути.
4. Ни один report не содержит raw secret values; допустимы только path,
   finding type, count и SHA inventory.
5. Existing Enriched-first, strict source/citation guards, Wiki-disabled
   default и late-fusion acceptance boundary сохраняются.
6. Текущее грязное дерево не очищается автоматически и не используется как
   основание для `READY_FOR_RELEASE`.

## 3. Phase 0 — credential incident handling

Это обязательная операторская предпосылка, но не автоматизируется в коде.

1. Владелец credentials отзывает и перевыпускает все текущие ключи из
   локального `D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid\.env`, если они ещё
   действительны.
2. Значения ключей не копируются в issue, report, test fixture, commit или
   новый документ.
3. В репозитории остаётся только `.env.example` с пустыми значениями.
4. Исторические/локальные credential-bearing snapshots не включаются в release
   bundle. Их обработка и retention — отдельное решение владельца данных.

Acceptance Phase 0: владелец предоставляет внешнее подтверждение rotation или
revocation. В evidence хранится только `risk_id`, статус и basis аттестации.

## 4. Phase 1 — fail-closed security scanner

### 4.1. Изменения

Изменить `scripts/security_release_scan.py`:

1. Сохранить отдельные поля:

   - `protected_files`: список путей и количества findings;
   - `protected_occurrences`: сумма findings в protected-файлах;
   - `tracked_protected_files`;
   - `staged_protected_files`.

2. `protected_files` не должен вычитать findings из общего security result.
   Отчёт обязан иметь однозначный derived status:

   ```text
   status=passed только если
   findings=0,
   protected_occurrences=0,
   tracked_protected_files=[] и
   staged_protected_files=[]
   ```

3. Exit code должен быть ненулевым при любом из условий:

   - `finding_files > 0`;
   - `occurrences > 0`;
   - `protected_occurrences > 0`;
   - unreadable/scanner errors;
   - protected filename tracked или staged.

4. `_security_inventory_is_fresh()` и `build_release_verification.py` должны
   проверять `protected_occurrences == 0` и отсутствие protected entries, а не
   только tracked/staged subsets.

5. В report запрещены raw token values. Проверка должна сохранять только
   sanitized path, count, pattern/category и inventory SHA.

6. Пустой или credential-free `.env` может существовать локально, но он должен
   быть явно отражён как protected file с нулевым количеством findings. Это не
   является silent allowlist для найденного секрета.

### 4.2. Regression tests

Добавить тесты в `tests/test_security_release_scan.py` и verifier tests:

- ignored `.env` с synthetic `sk-or-v1-...` → `protected_occurrences > 0`,
  `status=failed`, exit code `1`;
- report с `finding_files=0`, но `protected_occurrences > 0` → verifier false;
- empty `.env` → scanner не падает и не создаёт finding;
- raw secret не появляется в JSON report, exception text или stdout;
- tracked/staged `.env` блокирует gate независимо от содержимого;
- inventory freshness после изменения `.env` становится false.

Обязательно оставить regression на SQLite sidecar, binary и unreadable files.

## 5. Phase 2 — единый strict accepted-manifest consumer

### 5.1. Разрешённый API

Для `accepted_manifest_v2` единственным file-level entry point является:

```python
loader.manifest_contract.load_manifest_file(
    path,
    allow_legacy_manifest=False,
)
```

Он обязан использовать `strict_json_loads()`, exact allowed fields и полный
shared validation до любого report mutation, LightRAG mutation или release
identity stamping.

### 5.2. Consumers, которые необходимо перевести на API

Проверить и заменить обычный parsing release manifest в следующих путях:

- `loader/ingest.py`;
- `scripts/cold_provenance.py`;
- `scripts/build_release_evidence.py`;
- `scripts/build_release_verification.py`;
- `scripts/security_release_scan.py`;
- `scripts/sync_media_contract.py`;
- `scripts/migrate_portable_paths.py`;
- `scripts/validate_backup_restore.py` для `--release-manifest`;
- `scripts/build_release_risk_register.py`;
- `scripts/luna_reenrich_audit.py`;
- `scripts/build_release_test_evidence.py`;
- `scripts/late_fusion_activation_smoke.py`.

Внутренние backup/YouTube/golden manifests с другими schemas не смешивать с
accepted-manifest API: для них остаются отдельные validators.

### 5.3. Fail-closed правила

1. Duplicate JSON key, unknown root/identity/entry field, incomplete v2 identity,
   wrong profile/model/flags, path/hash/provenance mismatch — это hard failure.
2. Производитель не создаёт identity-bound report из неполного manifest.
3. `accepted_manifest_v1` допускается только через существующий явный legacy
   compatibility flag и не может попасть в release bundle.
4. После перевода consumers выполнить статический поиск: release paths не
   должны применять `json.loads()` к accepted manifest.

### 5.4. Regression tests

Для одного и того же malformed fixture проверить одинаковый fail-closed result
в loader, cold proof, security producer и verifier:

- duplicate root key;
- duplicate source-entry key;
- unknown root field;
- unknown `pipeline_identity` field;
- unknown source-entry field;
- incomplete `accepted_manifest_v2`;
- mismatched config identity;
- rejected path outside configured corpus roots.

Ни один такой fixture не должен вызывать LightRAG mutation или успешную запись
release report.

## 6. Phase 3 — устранение manifest TOCTOU

### 6.1. Требование

Ввести validated source snapshot между shared validator и loader. Точная форма
API свободна, но результат должен содержать как минимум:

- source id и canonical normalized path;
- validated normalized bytes/hash;
- validated card payload или exact card bytes/hash;
- exact `graph_text`, который будет передан LightRAG;
- `enriched_sha256` и `graph_text_sha256` для того же read snapshot.

`load_from_manifest()` не должен заново читать card path после успешной
валидации и использовать новый результат без повторной полной проверки.

Рекомендуемый вариант:

1. shared validator читает normalized/card files один раз;
2. создаёт immutable `ValidatedManifestEntry`;
3. loader получает из него exact graph text и canonical path;
4. `load_texts()` принимает уже прочитанные тексты и не открывает card повторно;
5. report сохраняет observed path только как диагностику, а identity строится
   по attempted canonical path.

Если реализация вынуждена перечитывать файл, она обязана сравнить SHA exact
bytes, используемых downstream, и прекратить загрузку при любом mismatch.

### 6.2. Конкурентность и границы

- Не полагаться только на `git clean`: corpus находится в ignored output и
  может измениться независимо от Git.
- Не считать `mtime` доказательством неизменности.
- При невозможности получить стабильный snapshot возвращать FAILED и не
  публиковать accepted report.

### 6.3. Regression tests

- изменить card между validation и load — loader завершает FAILED;
- изменить `graph_text` при сохранении JSON schema — hash mismatch блокирует
  insert;
- изменить normalized bytes после preflight — блокируется до LightRAG mutation;
- проверить, что exact `graph_text_sha256` в report совпадает с bytes,
  реально переданными loader;
- поздний failed status с basename продолжает удалять metadata только по
  attempted canonical identity.

## 7. Phase 4 — статическое окружение

Эта фаза не меняет production data. Нужно обеспечить воспроизводимый static
gate:

1. Установить project runtime из declared dependencies и dev dependencies,
   включая `streamlit` и `ruff`.
2. Зафиксировать в execution record:

   - `python --version`;
   - `pytest --version`;
   - `ruff --version`;
   - `pip show`/lock identity для ключевых packages.

3. Если проект выбирает lockfile, добавить его отдельным согласованным change
   и включить package-set SHA в release identity. Не подменять lockfile
   результатом случайного `pip freeze` без решения о поддерживаемой версии
   Python.
4. Test runner обязан выдавать machine-readable FAILED report при отсутствии
   `pytest`, `ruff` или обязательной runtime dependency; отсутствие команды не
   может трактоваться как skipped/pass.
5. Полный suite должен собираться без ручного `--ignore`.

## 8. Corpus run — отдельный самый последний шаг всего цикла

Полный production/cold/live прогон **не является последним пунктом этого ТЗ**
и не запускается автоматически сразу после его выполнения. Закрытие этого ТЗ
означает только завершение статических и regression-исправлений. После него
могут потребоваться дополнительные проверки, ревью и исправления проекта.

Corpus run нужно запускать только при сильной необходимости. Предпочтительный
момент — когда независимое read-only ревью показывает `0 P0 / 0 P1 / 0 P2` и
больше нечего исправлять, либо когда именно последний набор исправлений требует
подтверждения полным прогоном. Если ревью после этого ТЗ находит новую проблему,
полный прогон откладывается до завершения нового цикла исправлений и ревью; его
нельзя использовать как промежуточную диагностику после каждой правки.

До допуска к этой отдельной финальной операционной фазе должны быть завершены:

1. все исправления и regression/static checks из этого ТЗ;
2. дополнительные проверки проекта, выявленные после этого ТЗ;
3. исправления всех найденных проблем и повторное ревью;
4. отдельное решение о необходимости дорогого corpus run и execution-ТЗ.

Единственная допустимая последовательность уже допущенной финальной
операционной фазы:

1. Зафиксировать чистый release commit и lock/runtime identity.
2. Один раз сгенерировать финальный `accepted_manifest_v2`.
3. Выполнить accepted manifest load всех карточек и получить exact `N/N`.
4. На том же manifest выполнить cold LightRAG extraction и provenance gate.
5. Выполнить один live Late-Fusion smoke и собрать immutable bundle.
6. Если любой gate провален, следующий прогон делается только после нового
   решения о причинах и новой release identity; промежуточные исправления
   снова проходят только static/regression phase без полного corpus run.

До явного допуска этой фазы запрещены production load, full re-enrichment,
media backfill, cold extraction и live provider calls. Таким образом, дорогой
прогон является последним шагом всего цикла проверок проекта, а не частью
каждой итерации исправлений или автоматически последним шагом этого ТЗ.

## 9. Матрица обязательных проверок

| Finding | Negative case | Expected result |
|---|---|---|
| Security false-green | secret-like value в ignored `.env` | non-zero scanner и failed verifier |
| Security false-green | `protected_occurrences > 0`, findings=0 | verifier rejects |
| Manifest parser drift | duplicate/unknown v2 fields | одинаковый reject во всех consumers |
| Manifest parser drift | malformed manifest + producer | no stamped passed report |
| TOCTOU | card changes between proof/load | no LightRAG mutation, FAILED report |
| TOCTOU | graph text hash changes | exact hash mismatch, FAILED |
| Environment | missing `ruff`/`streamlit` | machine-readable failed static gate |
| Existing contract | basename cleanup after late failure | no stale metadata alias |
| Existing contract | official smoke mutation | `_smoke_is_strict()` false |
| Existing contract | extra physical bundle file | bundle/verifier reject |

## 10. Verification commands после реализации

Запускать только во внешнем temporary directory для pytest artifacts:

```powershell
$env:PYTHONUTF8 = "1"
python -m pytest -q -p no:cacheprovider --basetemp=<external-temp>
python -m ruff check loader scripts tests
python -m compileall -q loader scripts tests
git diff --check origin/master --
```

Дополнительно:

```powershell
python scripts/security_release_scan.py --report <external-report>
```

Ожидаемые результаты:

- полный test collection без collection errors;
- все regression tests passed;
- Ruff и compileall exit code `0`;
- security report не содержит protected occurrences;
- malformed-manifest и TOCTOU negative reproductions проходят;
- production/output/rag_storage/FTS/registry/Wiki state не изменены.

## 11. Definition of Done

ТЗ считается выполненным только если одновременно:

- security scanner и verifier fail-closed на `protected_occurrences`;
- текущий `.env` не содержит действующих credentials либо их rotation
  подтверждён владельцем; raw values отсутствуют в evidence;
- все accepted-manifest consumers используют общий strict loader;
- duplicate/unknown/incomplete manifest regression matrix зелёная;
- loader использует стабильный validated snapshot и не имеет доказательного
  TOCTOU между hash и actual LightRAG input;
- полный pytest собирается без `--ignore` и проходит;
- Ruff, compileall и diff-check проходят в зафиксированном окружении;
- повторное независимое read-only ревью находит **0 P0 / 0 P1 / 0 P2**;
- рабочее дерево и release identity остаются отдельным clean-check перед
  будущим release run.

После DoD разрешается подготовить отдельное execution-ТЗ. Этот документ сам
по себе не означает `READY_FOR_FINAL_RUN` и тем более `READY_FOR_RELEASE`.

## 12. Запрещённые shortcuts

- Не считать `protected_files` допустимым allowlist для ненулевых findings.
- Не пересчитывать только report SHA после ручного изменения payload.
- Не добавлять raw tokens в fixtures или evidence.
- Не использовать basename как source identity.
- Не лечить TOCTOU повторным best-effort чтением без проверки exact bytes.

## 13. Результат реализации 2026-08-23

- strict manifest loader подключён к release consumers; loader передаёт в
  LightRAG validated snapshot и блокирует изменения card/normalized bytes;
- security scanner и verifier fail-closed на `protected_occurrences`, а
  static runner сохраняет версии Python/pytest/ruff, package inventory и
  dependency-spec hashes;
- regression/static evidence after the final fix pass: `784 passed, 2 skipped,
  42 subtests passed`,
  Ruff, compileall и diff-check завершились с exit code `0`, runner `4/4`;
- отдельный read-only security scan текущего checkout завершился FAILED:
  физический `.env` содержит `3` protected occurrences. Значения не
  записывались в report и должны быть отозваны/заменены владельцем;
- полный corpus/card run, production load, cold extraction и live provider
  calls в рамках реализации не выполнялись. Они остаются отдельным самым
  последним шагом после дополнительных проверок, исправлений и финального
  решения о необходимости.
- Не подменять failed/missing dependency зелёным результатом.
- Не запускать production load, cold graph, live smoke, media backfill или
  push до независимого статического ревью после реализации.
