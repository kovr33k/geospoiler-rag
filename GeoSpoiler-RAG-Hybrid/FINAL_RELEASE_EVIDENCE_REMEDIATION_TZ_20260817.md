# ТЗ: исправление финального release-контура GeoSpoiler RAG

Дата: 2026-08-17
Статус до выполнения: `BLOCKED`
Цель: устранить дефекты доказательного и recovery-контура, после чего получить воспроизводимый релиз из чистого commit SHA.

## 1. Причина блокировки

Рабочие данные сейчас согласованы: 131 Enriched-карточка, 131 обработанный документ LightRAG, media sidecar/card mismatch = 0, Late-Fusion smoke = 10/10. Однако текущий `READY_FOR_RELEASE` нельзя считать доказанным по следующим причинам:

1. `scripts/build_release_evidence.py` меняет `run_id`, путь и SHA manifest-load отчёта после его выполнения. В результате пакет утверждает, что был загружен manifest из пакета, хотя загрузка выполнялась другим manifest с другим SHA.
2. `scripts/cold_graph_check.py` копирует готовый `rag_storage` и только пересчитывает статусы. Это warm-copy check, а не cold extraction.
3. `working_tree_sha256` учитывает только имена untracked-файлов, но не их содержимое. Восемь release-critical файлов сейчас untracked.
4. Итоговый verifier принимает недостаточно полные или нулевые отчёты и не пересчитывает часть заявленной идентичности из первичных данных.
5. Restore journal записывает target phase `moving`, но recovery не допускает эту фазу.
6. Финальный документ объявляет `READY_FOR_RELEASE`, хотя release commit не создан, рабочее дерево содержит 28 изменений, а evidence-пакет исключён из Git.

## 2. Обязательные инварианты

- Evidence builder никогда не изменяет содержимое входных отчётов.
- Каждый отчёт создаётся самим выполняющим инструментом уже с полной release identity.
- Один release run использует один byte-identical `accepted_manifest_v2`; именно он загружается, проверяется и входит в пакет.
- Любое отсутствие, несовпадение, нулевой placeholder или ошибка чтения даёт machine-readable `FAILED` и ненулевой exit code.
- Финальное одобрение относится только к чистому release commit SHA, а не к dirty working tree.
- Нельзя называть проверку cold extraction, если использовано копирование готового графа.
- Секреты не входят ни в commit, ни в staging allowlist, ни в публикуемый evidence.

## 3. Фаза A — неизменяемый evidence pipeline

### Реализация

1. Удалить из `build_release_evidence.py` переписывание `manifest.run_id`, `manifest_load.run_id`, `manifest_path` и `manifest_sha256`.
2. Копировать manifest и отчёты byte-for-byte. В `evidence_manifest.json` хранить их SHA-256 и размер.
3. Сначала создать конечный manifest с окончательным `release_run_id`, затем выполнить `main.py load --manifest` именно по этому файлу.
4. Loader должен сам записать в отчёт точные absolute/portable path, SHA manifest, run_id, expected/inserted/skipped/failed и ошибки.
5. Builder должен отклонять отчёт без собственной identity либо с identity, отличной от identity релиза; post-hoc stamping запрещён.

### Acceptance

- SHA manifest до загрузки, в loader report и в evidence bundle совпадает.
- Изменение одного байта любого исходного отчёта делает сборку/verification `FAILED`.
- Regression test подтверждает, что builder не меняет байты входных JSON.

## 4. Фаза B — полная release identity

### Реализация

1. Финальную identity строить от commit SHA и Git tree SHA при чистом дереве.
2. Pre-commit режим, если сохранён, обязан хешировать содержимое всех allowlisted untracked-файлов, а не только имена. Неallowlisted untracked-файлы блокируют запуск.
3. В `config_identity_sha256` включить все параметры, влияющие на retrieval/generation, без секретных значений:
   - query model/base URL и лимиты;
   - embedding provider/base URL/model/dimension;
   - Late-Fusion top-k, max sources, token/context limits, timeout;
   - hybrid/YouTube top-k;
   - reranker provider/model/top-N/candidate pool;
   - активные flags, media/STT models и contract versions.
4. API keys и токены в identity не включать; допустим только маркер `configured: true/false`.

### Acceptance

- Изменение содержимого untracked allowlisted файла меняет identity.
- Изменение любого result-affecting env-параметра меняет config identity.
- На clean commit повторный расчёт identity детерминирован.

## 5. Фаза C — fail-closed verifier со schema contracts

### Реализация

1. Для каждого типа evidence определить отдельную схему и обязательные поля; одного `status=passed` недостаточно.
2. Verifier обязан пересчитать:
   - SHA каждого evidence-файла;
   - `corpus_identity_sha256` из bundled manifest;
   - SHA normalized/card/graph_text каждого source по manifest;
   - количество manifest sources;
   - соответствие manifest/load/provenance/graph counts строго `N/N`, где `N > 0`.
3. Manifest load: `expected == inserted == len(manifest.sources)`, `skipped == failed == 0`, path/SHA/run_id обязательны и точны.
4. Provenance: `records == matched == len(manifest.sources)`, missing/ambiguous/nonterminal = 0.
5. Backup: `declared == checked > 0`, ошибки/коллизии/traversal/symlink = 0.
6. Portable paths: явный schema/status, scanned > 0, unresolved/changed = 0.
7. Smoke: ровно 10 уникальных кейсов; каждый `pipeline=late_fusion`, legacy fallback = false, citations valid, reference URLs valid; YouTube URL содержит корректный start time.
8. Tests: хранить exact command, exit code, passed/failed/skipped/subtests; `passed > 0`, `failed == 0`, exit code 0.
9. Любое исключение чтения JSON/SQLite/card/RAG ловить на верхнем уровне и всё равно атомарно писать FAILED report.

### Negative tests

- Пустые `{status: passed}`, 0/0 counts, отсутствующий schema, неверный SHA/path/run_id.
- Manifest с одним изменённым source SHA.
- Corrupt JSON, missing SQLite table, unreadable file.
- Smoke с fallback, citation error или девятью кейсами.
- Проверка должна завершаться ненулевым кодом и сохранять FAILED JSON.

## 6. Фаза D — настоящий cold LightRAG

### Реализация

1. Создать пустой изолированный `rag_storage`; копирование live storage запрещено.
2. Загрузить в него exact bundled manifest штатным loader API.
3. Дождаться terminal status каждого source и сверить source_id, content/graph hashes и processed count.
4. Отчёт должен содержать workspace path, manifest SHA, per-source status, duration, failures и признак `source_storage_copied=false`.
5. Существующий warm-copy check переименовать в `storage_open_smoke` и не использовать как cold gate.

### Acceptance

- Empty-before доказан отдельным preflight snapshot.
- Результат: `131/131 processed`, processing/failed/missing = 0.
- Прерывание или неполный прогон остаётся `FAILED`; его нельзя принять как риск и продолжить с `READY_FOR_RELEASE`.

## 7. Фаза E — restore и atomic durability

### Restore

1. Согласовать state machine: `planned -> moving -> moved -> copying -> copied -> verified`.
2. Recovery обязан разбирать фазу `moving` по фактическому наличию target/retired и безопасно откатывать неоднозначное состояние.
3. До первого move проверить полный backup manifest, SHA, пути и наличие всех источников.
4. Добавить fault-injection тест до/после каждого journal write, move, copy и verify; recovery запускать новым процессом.

### Atomic pair

1. После каждого `os.replace` выполнять best-effort fsync родительской директории с документированным Windows fallback.
2. В journal хранить old/new SHA обоих файлов.
3. Перед удалением committed journal проверять, что обе цели совпадают с new SHA; rollback проверять по old SHA.
4. Добавить power-loss/fault-injection тесты всех переходов.

## 8. Фаза F — security и media sync hardening

1. Ввести явный staging allowlist.
2. Security gate обязан отклонять protected secret file (`.env` и аналоги), если он tracked, staged или попал в allowlist, даже если scanner классифицирует его как protected.
3. Сканировать физический checkout, staged tree и итоговый commit tree; raw evidence/quarantine не публиковать.
4. `sync_media_contract.py` должен fail-closed учитывать invalid meta/card JSON и missing cards, писать errors и ненулевой exit code.
5. При `--apply` использовать durable atomic write и либо автоматически пересобирать graph_text/search_text/FTS/registry/LightRAG, либо выпускать machine-readable downstream-invalidated state, блокирующий релиз до rebuild.

## 9. Фаза G — release commit и публикуемые доказательства

1. Сформировать staging allowlist только из нужного кода, тестов, ТЗ и sanitized evidence metadata.
2. Raw logs, SQLite backups, quarantine, `.env`, media и временные каталоги оставить вне Git.
3. Публикуемые evidence manifest/report либо добавить в Git явно, либо сохранить во внешнем immutable хранилище; в tracked документе записать URI, SHA и retention policy.
4. Создать один release commit.
5. На этом commit повторить короткие offline gates, full tests, Ruff, compileall, security commit-tree scan и verifier.
6. `git status --porcelain` после проверки должен быть пустым; если verifier создаёт файлы, они пишутся вне checkout либо заранее предусмотрены.
7. Не изменять `docs/FINAL_VERIFICATION_20260817.md` после проверки release commit: machine verifier во внешнем immutable bundle является источником verdict. Если нужен итоговый текст `READY_FOR_RELEASE`, публиковать его во внешнем bundle или отдельным docs-only commit, который не выдаётся за проверенный release commit.

## 10. Обязательные тесты

- `tests/test_release_identity.py`: untracked content, config parameters, clean-tree identity.
- `tests/test_build_release_evidence.py`: byte-preserving bundle, cross-run rejection.
- `tests/test_build_release_verification.py`: все positive и negative schema/cross-count cases.
- `tests/test_cold_graph_check.py`: empty storage, no copy, partial failure.
- `tests/test_luna_reenrich_control.py`: crash/recovery для каждой restore phase, включая `moving`.
- `tests/test_atomic_pair.py`: journal/hash/fsync/fault transitions.
- `tests/test_security_release_scan.py`: staged/tracked `.env`, commit-tree scan.
- `tests/test_sync_media_contract.py`: invalid/missing inputs и downstream invalidation/rebuild.

## 11. Финальный DoD

Релиз считается закрытым только если одновременно выполнено следующее:

1. Exact manifest byte-identical во всех стадиях, и именно он загружен.
2. Evidence builder ничего не переписывает; все отчёты имеют исходную валидную identity.
3. Verifier проходит negative-test suite и не принимает 0/0 или неполные payloads.
4. Настоящий cold extraction: 131/131.
5. Live graph/provenance/media/FTS/registry gates согласованы.
6. Restore recovery и atomic-pair fault tests зелёные.
7. Security scan checkout/staging/commit: 0 findings; secret files не tracked/staged.
8. Full pytest, Ruff, compileall и diff check зелёные.
9. Release-critical untracked files = 0; рабочее дерево чистое.
10. Финальный immutable report привязан к release commit SHA и доступен ревьюеру.

До выполнения всех десяти пунктов допустимый итоговый статус — только `BLOCKED`, не `READY_FOR_RELEASE`.
