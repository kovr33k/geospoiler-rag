# ТЗ: закрытие остаточных release-блокеров GeoSpoiler RAG

Дата: 2026-08-17
Статус: `BLOCKED` до выполнения всех DoD ниже.
Область: только доказательный release-контур, isolated cold-graph и публикация
кода/evidence. Production `output/`, `rag_storage/`, media-cache и credentials
не изменять во время ревью и подготовки плана.

## 1. Результат текущего ревью

### Подтверждено

- Полный pytest: `699 passed, 1 skipped, 42 subtests`, 2 warnings.
- Release-focused tests: `98 passed`, 3 subtests.
- Ruff и `compileall`: успешно.
- `git diff --check`: только штатные предупреждения о CRLF, содержательных ошибок нет.
- Текущий corpus: `129` normalized sidecars, `131` Enriched cards.
- Media contract: `95/95` media downloaded, visual `95/95 success`, STT `21 transcribed + 2 no_speech`, card/sidecar mismatch `0`.
- Security scan checkout: `9288` файлов, `0` finding files, `0` occurrences, `0` unreadable; tracked/staged protected files отсутствуют.
- Live graph сейчас содержит `131/131 processed`, но это само по себе не доказывает загрузку именно текущего manifest.
- Ротация старого токена считается закрытой по явной пользовательской аттестации; не возвращать этот пункт в блокеры без новой информации.

### Найдено

#### P1 — текущий manifest не связан с фактической загрузкой

Сгенерирован актуальный manifest:

- `run_id=release_20260817_remediation_manifest`;
- SHA-256 `c6c902991e5c0cccbf3cbccf1a1219ee2c65676c21ec1f687852e8d4ec1fb946`;
- `131` source entries.

Текущий `artifacts/accepted_manifest_load_report.json` относится к другому
запуску:

- `run_id=accepted_manifest_v2`;
- SHA-256 `d94213208ff64cadb338c7ea404c101b030b61f506082f48632a319f6fc7ed66`;
- manifest path `artifacts/release_20260817_work/accepted_manifest_v2.json`.

Ни один найденный report/evidence-файл не содержит SHA текущего manifest.
Следовательно, `131 processed` в live graph не является доказательством
загрузки текущего corpus. До устранения это запрещает release.

#### P1 — настоящий cold extraction не завершён

Изолированный прогон был остановлен после наблюдения высокой latency:

- `source_storage_copied=false`;
- `empty_before=true`;
- `expected=131`;
- `processed=1`;
- `processing=1`;
- `complete=false`;
- итоговый partial report имеет `status=failed`.

Это полезное диагностическое доказательство, но не cold-gate. Старый пакет
`release_20260817_evidence_final5` при повторной проверке новым verifier уже
отклонён (`clean_release_tree`, `manifest_identity`, `portable_paths`,
`security`, `cold_graph`, `tests`). Его нельзя переименовать в успешный.

#### P1 — release не привязан к чистому commit и публикуемому evidence

Рабочее дерево содержит `33` porcelain-записи, включая untracked release-код,
тесты и документы. `artifacts/` исключён через `.git/info/exclude`, поэтому
текущий manifest и отчёты не входят в проверяемый Git commit. Нужен отдельный
release commit (или внешний immutable evidence URI с SHA, записанный в
tracked-документе); dirty snapshot не может быть `READY_FOR_RELEASE`.

#### P2 — cold runner не гарантирует machine-readable FAILED при прерывании

`scripts/cold_graph_check.py` вызывает `asyncio.run()` без верхнеуровневого
перехвата исключения/`KeyboardInterrupt`. При остановке процесса report может
не быть создан самим runner; текущий partial report был оформлен оператором
вручную. Для воспроизводимого gate runner обязан атомарно писать `FAILED`
report с уже известными counts и причиной прерывания.

#### P2 — cold report недостаточно аудируем

Текущий report содержит aggregate counts, но не обязательные per-source
terminal status, source id, duration и error details. Эти поля нужны для
доказательства, что каждый из `N` источников завершился именно в terminal
`processed`, а не только что aggregate counter достиг `N`.

#### P2 — dry-run media sync двусмысленен

`sync_media_contract.py` возвращает `status=passed`, если найдено `candidates`
в dry-run режиме. Это не ошибка данных, но оператор может принять report за
состояние без pending changes. Нужен явный статус `changes_pending` (либо
ненулевой exit code) при `candidates > 0` и `apply=false`.

## 2. Обязательный план исправления

### Фаза A — зафиксировать exact release identity

1. На выбранном commit создать новый уникальный `release_run_id`.
2. Сгенерировать `accepted_manifest_v2` один раз; после генерации не менять
   normalized/enriched corpus или конфигурацию.
3. Сохранить SHA и byte size manifest в machine-readable preflight report.
4. Любой report с другим `run_id`, path или SHA немедленно считать invalid.
5. До live load проверить чистоту/allowlist Git tree; release-critical
   untracked files не допускаются.

### Фаза B — загрузить именно этот manifest

1. Выполнить штатный loader строго с exact path manifest из Фазы A.
2. Loader report обязан содержать тот же `run_id`, абсолютный/канонический
   path, тот же SHA, `expected=inserted=N`, `skipped=failed=0`, `errors=[]`.
3. Пересчитать graph status, provenance и metadata-index после загрузки.
4. Не принимать старый `accepted_manifest_load_report.json`; новый report
   должен быть создан самим loader, а не post-hoc исправлен.

### Фаза C — сделать воспроизводимый cold gate

1. Создать новый внешний isolated workspace с отсутствующим `rag_storage`;
   копирование live storage запрещено.
2. Добавить в `cold_graph_check.py` верхнеуровневый fail-safe:
   `KeyboardInterrupt`/исключение превращается в атомарный `FAILED` report,
   процесс завершается ненулевым кодом, partial workspace сохраняется для
   диагностики и не считается успешным.
3. Добавить проверку manifest SHA/run_id перед стартом и в итоговый report.
4. Добавить per-source records: `source_id`, `doc_id`, `status`, `duration_ms`,
   `content_hash`/`graph_hash` и безопасное (без секретов) поле `error`.
5. Если один документ занимает минуты, реализовать bounded batch/resume:
   checkpoint должен быть привязан к тому же manifest SHA, а повторный запуск
   обязан пропускать только уже подтверждённые terminal `processed` записи.
   Нельзя продолжать с изменённым manifest.
6. Выполнить прогон до `N/N`; partial/processing/failed/missing = `FAILED`.

### Фаза D — собрать свежий identity-bound evidence

Для одного и того же `release_run_id` заново получить:

- manifest и loader report;
- backup/restore validation;
- portable-path report;
- media-contract reconciliation;
- provenance/graph/FTS/registry report;
- Late-Fusion smoke 10/10 с citations, URL references и fallback=0;
- security scan checkout + staged/commit tree;
- full pytest, Ruff, compileall, diff-check;
- завершённый cold report.

Каждый report должен иметь `release_schema_version`, ту же `release_identity`,
команду запуска и `errors=[]`. Evidence builder обязан копировать входные
байты без переписывания. Verifier должен пройти на этом пакете; старые
`final5` snapshots не смешивать с новым run.

### Фаза E — опубликовать только чистый release

1. Сформировать staging allowlist: source code, tests, актуальные ТЗ и
   sanitized evidence metadata/reports.
2. Raw SQLite, quarantine, media, logs, `.env` и временные cold workspaces не
   добавлять в Git.
3. Поскольку `artifacts/` игнорируется, либо force-add только утверждённый
   sanitized allowlist, либо записать в tracked-документ внешний immutable URI,
   SHA и retention policy.
4. Создать один release commit и не менять его после получения evidence.
5. Повторить verifier уже на commit SHA; `git status --porcelain` должен быть
   пустым.
6. Только после успешного verifier обновить
   `docs/FINAL_VERIFICATION_20260817.md` на `READY_FOR_RELEASE`.

## 3. Обязательные регрессионные тесты

- `cold_graph_check`: exception/KeyboardInterrupt -> атомарный FAILED report;
- cold report: exact manifest SHA/run_id, per-source records и partial failure;
- end-to-end manifest/load: mismatch SHA/run_id/path отклоняется;
- verifier: старый `final5` пакет и новый manifest cross-run отклоняются;
- `sync_media_contract`: dry-run с candidates возвращает `changes_pending` и
  не маскируется под clean pass;
- clean-tree/evidence allowlist: untracked content или ignored evidence,
  отсутствующий в allowlist, блокирует release;
- smoke/security/test report: отсутствие schema/identity/errors блокирует
  bundle.

## 4. Финальный DoD

Релиз разрешён только если одновременно выполнены все пункты:

1. Один exact manifest byte-identical на стадиях generation/load/bundle.
2. Loader report и graph относятся к этому manifest SHA/run_id.
3. Cold graph стартовал пустым и завершил `N/N processed`, без
   `processing/failed/missing`, с per-source evidence.
4. Verifier проходит на свежем identity-bound bundle; старые snapshots не
   используются.
5. Media/card, provenance, registry, FTS и citations gates зелёные.
6. Security checkout/staging/commit scan: `0` findings, secrets не tracked и
   не staged.
7. Full tests, Ruff, compileall и diff-check зелёные.
8. Release commit чистый: `git status --porcelain` пуст.
9. Публикуемый evidence доступен ревьюеру по tracked пути или immutable URI,
   с SHA и retention policy.
10. До выполнения всех пунктов итоговый статус только `BLOCKED`.
