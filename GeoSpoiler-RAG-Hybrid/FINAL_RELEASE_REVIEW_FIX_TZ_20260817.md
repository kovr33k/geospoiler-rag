# ТЗ: закрытие замечаний финального release-review

Дата: 2026-08-17
Ветка: `work/rag-v2-snapshot-20260810`
Базовый commit: `64bb162edcecd13f251a054349af03e47975b2dc`
Стартовый verdict: `BLOCKED`

## 1. Цель

Исправить все подтверждённые дефекты release-контура после реализации
`FINAL_RELEASE_CLOSEOUT_TZ_20260816.md`, затем получить один воспроизводимый набор
evidence с общей identity и verdict `READY_FOR_RELEASE`.

Это ТЗ не разрешает push, merge, tag или force-операции. Production-запись,
холодная сборка LightRAG и live Late-Fusion smoke выполняются только после
завершения code-gates и создания проверенного SHA-backup.

## 2. Подтверждённые findings

### F1, P1: final verifier несовместим со штатными report-схемами

`scripts/build_release_verification.py` читает:

- `provenance.counts.records`, хотя `graph_provenance_reconciliation_v1` хранит
  `records` в корне;
- `portable.status` и `portable.changed`, хотя `portable_path_migration_v1`
  хранит `changed_files`, `field_changes`, `unresolved` и не имеет `status`.

Следствие: verifier не может дать PASS на корректно сгенерированных свежих
provenance/portable reports.

### F2, P1: evidence не связано одной release identity

Verifier принимает независимые JSON по имени и локальным полям `status/passed`,
но не требует общего `release_run_id`, git commit/tree, manifest SHA, config SHA и
corpus SHA. `test_evidence.json` принимается по одному `status=passed`.
`accepted_manifest_load_report.json` не сверяется с SHA выбранного manifest.

Следствие: старый либо вручную созданный успешный report может быть смешан с
новым corpus/manifest. Это нарушает пункты 6.5, 12 и 14.14 исходного release-ТЗ.

### F3, P1: restore не восстанавливается после завершения процесса

`scripts/luna_reenrich_control.py:restore()` откатывает изменения только внутри
текущего `except`. После выключения компьютера повторный запуск перезаписывает
существующий незавершённый `restore_journal.json` новым `prepared`. Сам journal
записывается неатомарно и не хранит достаточный per-target state для безопасного
resume/rollback.

### F4, P1: повреждённый atomic-pair journal скрывает карточку

`normalizer/transcription_backfill.py` и `normalizer/visual_backfill.py` ловят
`RuntimeError` из `recover_atomic_pair()` и делают `continue`. Карточка исчезает
из `candidates_seen`, failure-report и acceptance accounting. Комментарий
утверждает fail-closed поведение, но фактически ошибка проглатывается.

Дополнительно journal в `normalizer/atomic_pair.py` перезаписывается напрямую;
авария во время записи может повредить единственную recovery-копию.

### F5, P1: security scan пропускает SQLite sidecars

`scripts/security_release_scan.py` без проверки исключает все `-wal`, `-shm` и
`-journal`. В текущем `artifacts/` такие файлы существуют. Токен в WAL даёт
`scanned_files=0`, `finding_files=0` для этого файла.

### F6, P1: Luna audit не выполняет собственный identity-контракт

`scripts/luna_reenrich_audit.py` всё ещё:

- не сохраняет git commit/tree, config identity, corpus manifest SHA и command;
- разрешает неявный auto-generated run id вместо обязательного write run id;
- не имеет настоящего read-only stdout режима без записи.

### F7, P2: legacy accepted manifest имеет слабую identity

`accepted_manifest_v1` проверяет только `profile`. Model, media contract, flags и
expected count не связаны. Release verifier требует v2, но штатный loader всё ещё
может загрузить v1 без явного legacy-разрешения.

### F8, release gates: свежие live-evidence ещё не выполнены

- isolated cold LightRAG extraction N/N отсутствует;
- новый 10-case Late-Fusion smoke не запускался;
- `test_evidence.json` exact release checkout отсутствует;
- рабочее дерево содержит незакоммиченные и untracked release-critical файлы.

## 3. Общий release evidence contract

Ввести `release_evidence_v1`. Каждый publishable report обязан содержать:

- `schema_version`;
- `release_run_id`;
- `generated_at` в UTC;
- `command` без секретов;
- `git_commit` и `git_tree_sha256` либо детерминированный tree identity;
- `config_identity_sha256`;
- `manifest_path` и `manifest_sha256`;
- `corpus_identity_sha256`;
- `status` из закрытого enum `passed|failed`;
- `errors` как list.

Все evidence перечисляются в одном `evidence_manifest.json` с относительным
путём, bytes и SHA-256. Verifier сначала валидирует manifest и общую identity,
затем читает отдельные отчёты. Поиск «последней папки» по mtime запрещён для
release approval; `--input-dir` и `--release-run-id` обязательны.

## 4. Фаза A: исправить verifier и схемы

1. В `scripts/build_release_verification.py` использовать реальные схемы:
   `provenance.records`, `provenance.counts.*`,
   `portable.changed_files/field_changes/unresolved`.
2. Валидировать `schema_version` каждого report и отклонять неизвестные поля
   статуса, типы и неполные payload.
3. Связать load report с manifest по `run_id`, canonical path и manifest SHA.
4. Связать все reports через `release_evidence_v1`.
5. Удалить fallback на hardcoded имена `graph_activation_20260817.json` и
   `accepted_risk_register_20260812.json`. Использовать только evidence manifest.
6. Invalid/missing optional JSON должен формировать failed report и exit 1, а не
   traceback без output.
7. Не открывать SQLite через режим, способный создать отсутствующий файл.
   Проверить `is_file`, затем открыть read-only URI и выполнить integrity check.
8. Добавить `tests/test_build_release_verification.py`.

Acceptance:

- штатные fresh provenance и portable reports проходят;
- forged/stale report без общей identity отклоняется;
- manifest SHA mismatch отклоняется;
- `{"status":"passed"}` не считается test evidence;
- missing/invalid evidence создаёт machine-readable FAILED report и exit 1;
- успешный synthetic bundle даёт PASS только при общей identity.

## 5. Фаза B: crash-safe restore

1. Сделать запись restore journal атомарной: temp, fsync file, replace, fsync dir.
2. Journal хранит для каждого target:
   `planned`, `moved`, `copying`, `copied`, `verified`, `rolled_back`, а также
   exact target, retired path, backup path и expected SHA records.
3. До создания нового journal проверять существующий:
   - `committed`/`rolled_back` разрешают новый запуск;
   - любой nonterminal journal блокирует обычный restore и запускает только
     deterministic recovery/rollback;
   - повреждённый journal блокирует операции без filesystem mutation.
4. Recovery сначала проверяет фактические paths/SHA, затем либо завершает
   verified copy, либо возвращает все moved targets и удаляет только targets,
   созданные этим journal.
5. Добавить fault-injection tests после каждого move/copy/journal update, включая
   имитацию нового процесса.

Acceptance: после прерывания на каждой фазе повторный запуск восстанавливает
исходное состояние с теми же SHA; существующий journal никогда не
перезаписывается до recovery.

## 6. Фаза C: atomic-pair fail-closed

1. Записывать atomic-pair journal через атомарный replace, не прямым overwrite.
2. Проверять schema, phase, target paths и base64 до изменения target-файлов.
3. Не ловить recovery failure через silent `continue`.
4. Возвращать/выбрасывать typed failure, включать path в machine report и
   завершать backfill nonzero до live egress.
5. Повреждённый journal сохранять для диагностики либо переносить в явный
   quarantine после SHA-manifest; не удалять молча.

Acceptance: malformed/truncated/wrong-schema journal даёт reported failure,
candidate не исчезает из accounting, production text/meta не меняются.

## 7. Фаза D: полный security scan

1. Не пропускать `-wal`, `-shm`, `-journal`.
2. Для SQLite main DB + sidecars выбрать один fail-closed алгоритм:
   read-only snapshot/checkpoint в изолированном каталоге либо raw-byte scan всех
   sidecars с обязательным учётом в report.
3. Report содержит `candidate_files`, `scanned_files`, `skipped_files`,
   `unreadable_files`, тип каждого файла и ошибки. Gate: skipped=0,
   unreadable=0, findings=0.
4. Добавить тест с credential-like marker только в WAL и тест с unreadable file.
5. Pytest scratch обязан находиться вне repo; текущие retained scratch artifacts
   либо санитизировать/перенести по отдельному manifest, либо явно включить в scan.

## 8. Фаза E: Luna audit и manifest policy

1. `luna_reenrich_audit.py`:
   - `--run-id` обязателен для записи;
   - `--stdout-only` не пишет файлов;
   - report реализует `release_evidence_v1`;
   - output dir append-only и не может существовать заранее.
2. Loader по умолчанию принимает для production только
   `accepted_manifest_v2`.
3. V1 разрешается только отдельным CLI-флагом `--allow-legacy-manifest`, пишет
   warning/report `legacy_identity_not_release_eligible` и никогда не может
   участвовать в release verifier.
4. Добавить negative tests для v1 без explicit flag и identity drift.

## 9. Фаза F: verification после code fixes

Выполнить без live network/LLM egress:

1. targeted tests для verifier/restore/atomic/security/manifest/audit;
2. полный pytest с заранее созданным внешним Windows basetemp;
3. Ruff, compileall, `git diff --check`;
4. security scan с `skipped=0`, `unreadable=0`, findings=0;
5. synthetic end-to-end release bundle: один PASS и полный набор negative cases;
6. обновить README и `docs/FINAL_VERIFICATION_20260817.md` только по факту.

Code gate: 0 открытых P1/P2, все тесты зелёные. До этого cold graph и live smoke
не запускать.

## 10. Фаза G: production/live closeout

После code gate и SHA-backup:

1. создать новый уникальный `release_run_id` и пустой evidence directory;
2. записать baseline/config/corpus identities;
3. выполнить isolated cold LightRAG extraction для всех 131 manifest sources;
4. проверить processed N/N, provenance N/N, stale/fake paths 0;
5. активировать candidate только через проверенный backup/restore workflow;
6. выполнить 10-case Late-Fusion smoke: pipeline 10/10, fallback 0,
   citations valid, reference URL valid, YouTube start URL present в нужном case;
7. запустить exact-checkout test command и записать подписанное test evidence;
8. собрать `evidence_manifest.json`, затем final verifier;
9. final report обязан иметь verdict ровно `READY_FOR_RELEASE` или `BLOCKED`.

## 11. Фаза H: clean release preparation

1. Сформировать explicit staging allowlist.
2. Не stage: `.env`, runtime SQLite/WAL/SHM, raw backups, quarantine, logs,
   caches, pytest scratch.
3. Stage: source, tests, docs и только sanitized evidence из allowlist.
4. Проверить отсутствие release-critical untracked files.
5. Создать один release commit только после PASS всех gates.
6. Повторить short verification уже на commit SHA.
7. Push/tag/merge выполнять только по отдельной команде пользователя.

## 12. Финальный Definition of Done

Релиз закрыт только если одновременно:

1. full pytest, Ruff, compileall и diff-check зелёные;
2. verifier-schema compatibility покрыта тестами;
3. все evidence имеют одну identity и SHA-manifest;
4. restore проходит process-crash recovery tests;
5. atomic-pair recovery failures видимы и fail-closed;
6. security scan: skipped=0, unreadable=0, findings=0, WAL/SHM/journal учтены;
7. production loader использует manifest v2;
8. cold LightRAG: 131/131 processed и provenance 131/131;
9. media/card mismatches 0, blocking flags 0;
10. Late-Fusion smoke 10/10, fallback 0, citations/URL valid;
11. token rotation закрыта по user attestation;
12. working tree clean, release-critical untracked files 0;
13. final machine report status `passed`, Markdown verdict
    `READY_FOR_RELEASE`;
14. открытых неисправленных P1/P2 нет.

Любое невыполненное условие оставляет verdict `BLOCKED`.
