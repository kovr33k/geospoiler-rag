# ТЗ: закрытие findings post-review release evidence

Статус: **SUPERSEDED — BLOCKED BY R2 REVIEW**. Повторное ревью выявило
остаточные P1/P2 в bundle trust boundary, test receipts, state freshness,
smoke ledger и atomic publication. Актуальные findings и ТЗ:
`RELEASE_EVIDENCE_STATIC_REVIEW_R2_20260818.md` и
`RELEASE_EVIDENCE_STATIC_FIX_TZ_R2_20260818.md`.

Цель: исправить все findings из
`docs/RELEASE_EVIDENCE_POST_REVIEW_20260818.md` и только после повторного
статического ревью вернуть статус `READY_FOR_FINAL_RUN`.

## Жёсткие ограничения

- В этом ТЗ не запускать production manifest load, cold extraction, live
  Late-Fusion smoke, media backfill, полный pytest и новый release bundle.
- Не делать commit, push, tag или deploy.
- Не менять production `output/`, `rag_storage/`, FTS/registry SQLite и
  credentials.
- Все проверки выполнять на fixtures и временных каталогах вне production.

## Фаза 1. Единый строгий schema registry

1. В `scripts/release_identity.py` определить versioned registry:
   `kind -> producer -> schema_version -> required/allowed fields`.
2. `stamp_report()` должен принимать явные `producer`, `schema_version`,
   `source_inputs` и настоящий `command_argv: list[str]`; убрать `setdefault`
   для identity-critical полей.
3. Проверять UTC timestamp, типы, отсутствие unknown critical fields,
   manifest/corpus SHA в `source_inputs`, release identity и payload SHA.
4. Builder и verifier обязаны использовать один validator, а не две похожие
   проверки.
5. Risk register: ровно один уникальный `RISK-TOKEN-ROTATION-001`, status
   `closed`, completion basis `user_attestation`; значение секрета запрещено.

Acceptance:

- forged producer/schema/source_inputs reproduction отклоняется;
- producer и verifier не расходятся по одной и той же fixture;
- unit tests покрывают missing, duplicate, unknown и wrong-type fields.

## Фаза 2. Canonical self-contained bundle

1. Builder принимает ровно обязательные kinds:
   `manifest_load`, `provenance`, `security`, `backup`, `portable_paths`,
   `cold_graph`, `late_fusion_smoke`, `tests`, `risk_register`.
2. Запретить reserved kinds `accepted_manifest`, `run_index`,
   `evidence_manifest`, duplicate sanitized filenames и duplicate resolved
   paths.
3. `run_index` сделать обязательным для verifier. Проверять schema, run ID,
   manifest SHA, точный report-kind set и canonical attempt name
   `attempt-NNN`.
4. Скопировать в bundle raw test logs, test summary и smoke attempt ledger;
   включить каждый файл в `evidence_manifest.json` с bytes/SHA.
5. Builder не должен возвращать `passed`, если собранный bundle не проходит
   собственный `_evidence_bundle` preflight.

Acceptance:

- incomplete/unknown/reserved/colliding kinds отклоняются до публикации;
- bundle не зависит от внешних абсолютных путей;
- удаление staging после сборки не ломает verifier.

## Фаза 3. Достоверный test gate

1. Заменить ручной ввод exit codes на runner, который сам запускает и пишет
   четыре фиксированные команды: pytest, Ruff, compileall, base-aware
   `git diff --check`.
2. Для каждой команды сохранить argv, cwd, start/end UTC, duration, exit code,
   stdout/stderr bytes и SHA.
3. Summary строить самим из результата runner или хранить его исходный файл с
   SHA; ручное расхождение summary/log/exit code должно блокировать gate.
4. Verifier читает только bundled logs и сверяет bytes/SHA/exit code/command.

Acceptance:

- подмена exit code, summary, argv или одного байта log блокирует verifier;
- отсутствующий log не может считаться passed.

## Фаза 4. Независимый cold proof

1. До load вычислить SHA реальных normalized bytes, enriched card bytes и
   точного `graph_text` из файлов; сравнить с manifest.
2. После load для каждого source получить фактический doc_id/status и SHA
   загруженного full-doc content; сравнить с graph_text SHA.
3. Сохранить deterministic inventory всех cold storage файлов: relative path,
   bytes, SHA. Запретить symlink/path escape и незаявленные storage copies.
4. Развести метрики:
   `inserted_by_loader`, `reused_processed`, `processed_terminal`; удалить либо
   переименовать двусмысленный `inserted`.
5. Lineage sets должны быть непересекающимися, уникальными и в точности
   покрывать manifest source IDs; checkpoint хранит ту же identity и inventory
   chain.

Acceptance:

- отсутствующий или изменённый source/card/graph/full-doc блокирует cold gate;
- SHA нельзя получить простым копированием из manifest;
- resume fixture доказывает реальные inserted/reused sets.

## Фаза 5. Обязательный smoke attempt ledger

1. Ledger path выводить из release run directory; CLI override для release
   mode запретить. Каждая попытка получает монотонный `attempt-NNN`.
2. Записывать попытку атомарно до возврата при success, validation failure,
   exception, timeout и KeyboardInterrupt.
3. Использовать append-only отдельные attempt files либо hash chain
   `previous_attempt_sha256`; current attempt обязан быть последним.
4. В case evidence сохранять citation IDs, reference IDs и sanitized URLs.
   Verifier заново проверяет mapping, URL validity, fallback и pipeline.
5. Предыдущая failed attempt может быть исправлена последующей, но не может
   исчезнуть из ledger/bundle.

Acceptance:

- fresh singleton ledger после ранее записанной failed attempt отклоняется;
- исключение оставляет валидный failed attempt artifact;
- удаление/перестановка attempt ломает chain SHA.

## Фаза 6. Freshness и exact state reconciliation

1. На момент verdict повторно проверить каждый manifest entry: existence,
   normalized/enriched/graph_text SHA и exact source ID/path.
2. Построить exact expected doc_id/source_id sets и сравнить с текущими
   LightRAG status/full_docs/metadata index. Одних counts недостаточно.
3. Provenance либо пересчитывать внутри verifier, либо связывать report с SHA
   текущего RAG inventory и повторно сверять этот SHA.
4. Добавить blocking checks для FTS/registry: expected card/source rows,
   YouTube/Wiki accounting, references count и `references_with_urls ==
   references`.
5. `state_inventory` должен участвовать в `checks`, быть связан с manifest и
   вычисляться streaming SHA без `read_bytes()` для больших файлов.

Acceptance:

- замена одного processed doc другим при том же count блокируется;
- изменение ignored card/RAG/SQLite после producer report блокируется;
- нулевые или stale FTS/registry больше не могут дать passed verdict.

## Фаза 7. Failure-path и атомарность

1. Определить полный failure-report schema, который безопасно рендерится в
   JSON/Markdown при исключении на любом этапе.
2. Все producer reports писать через общий unique-temp + flush + fsync +
   `os.replace`; при необходимости синхронизировать parent directory.
3. Добавить regression на текущий `KeyError: 'total'`; verifier обязан
   опубликовать FAILED report и вернуть ненулевой exit code.
4. Финальный verdict снабдить payload SHA или маленьким
   `verification_manifest.json`, который хеширует JSON и Markdown.
5. Параллельные writers должны либо блокироваться lock-файлом, либо получать
   разные attempt directories.

Acceptance:

- fault injection на каждом write/rename не оставляет passed/truncated output;
- exception path всегда создаёт читаемый FAILED artifact.

## Фаза 8. Regression suite и повторное статическое ревью

Обязательные negative tests:

- forged envelope/source inputs;
- incomplete, reserved и colliding bundle kinds;
- missing/invalid run index;
- external or modified raw log;
- missing cold source и mismatched loaded content;
- hidden/rewritten smoke attempt;
- stale provenance при неизменном count;
- broken FTS/registry;
- verifier exception и interrupted atomic write.

Запустить только затронутые fixture/unit tests, Ruff, compileall и
`git diff --check`. Полный suite и любой live/cold/production run в этой фазе
запрещены.

## Статический DoD

- Все P1/P2 из post-review закрыты кодом и negative tests.
- Producer, builder и verifier используют один exact schema registry.
- Bundle самодостаточен, run index обязателен, external staging после сборки не
  нужен.
- Cold SHA и текущий state измеряются независимо, а не копируются из evidence.
- Smoke failures и retries невозможно скрыть.
- Verifier на любой ошибке публикует FAILED artifact, а не падает.
- Повторное read-only ревью не находит P0/P1/P2.

Только после этого обновить статус на `READY_FOR_FINAL_RUN` и создать отдельное
execution-ТЗ для единственного полного release/cold/live прогона.

## Результат выполнения

- Единый registry producer/schema/source-inputs используется producer-ами,
  bundle builder-ом и verifier-ом; forged envelope, missing/unknown/wrong-type
  поля и повторный resolved report path блокируются.
- Bundle теперь самодостаточный: ровно девять report kinds, обязательный
  `run_index`, canonical `attempt-NNN`, raw test logs/summary и smoke attempt
  ledger с SHA/bytes в `evidence_manifest.json`. Builder выполняет собственный
  preflight перед возвратом `passed`.
- Test evidence формируется runner-ом с argv/cwd/временем/длительностью/
  exit-code/SHA для pytest, Ruff, compileall и `git diff --check`; ручные
  exit-codes для release-режима отклоняются.
- Cold proof измеряет normalized/enriched/graph_text и загруженный full-doc
  независимо от manifest, проверяет exact ID sets, lineage и storage inventory;
  неоднозначный `inserted` в release gate запрещён.
- Smoke attempts атомарны, связаны hash-chain, прошлые ошибки не исчезают;
  ledger path выводится из release run directory и проверяется verifier-ом.
- Failure path verifier публикует читаемый FAILED JSON/Markdown и
  `verification_manifest.json`; producer reports используют общий durable
  atomic writer.

Проверки выполнения на fixtures/локальном коде:

- `pytest` затронутого release/cold/identity контура: **83 passed, 2 skipped,
  2 warnings**;
- Ruff: **All checks passed**;
- py_compile всех затронутых модулей: **passed**;
- `git diff --check`: **passed** (только штатные CRLF warnings Git).

Production manifest load, media backfill, cold extraction, live Late-Fusion
smoke, полный pytest и новый release bundle в этой фазе не выполнялись. Их
единственный следующий запуск описан в
`docs/RELEASE_EVIDENCE_FINAL_EXECUTION_TZ_20260818.md`; до него
`READY_FOR_FINAL_RUN` не означает `READY_FOR_RELEASE`.
