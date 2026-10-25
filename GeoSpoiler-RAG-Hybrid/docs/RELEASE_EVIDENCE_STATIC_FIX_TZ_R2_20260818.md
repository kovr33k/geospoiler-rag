# ТЗ: статическое закрытие release-evidence findings R2

Статус: **IMPLEMENTED — STATIC CHECKS PASS; FINAL RUN FORBIDDEN UNTIL RE-REVIEW**

Результат реализации: общие manifest/report validators, trust-boundary и
atomic bundle publication, authentic test receipts, smoke hash-chain,
provenance/state reconciliation и regression tests внесены. Целевой static
набор: **133 passed, 1 skipped, 3 subtests**; Ruff, `py_compile` и `git
diff --check` проходят. Production data, live providers и final release run не
затрагивались.

Источник: `docs/RELEASE_EVIDENCE_STATIC_REVIEW_R2_20260818.md`.

## Цель

Закрыть все 8 P1 и 2 P2 статически и regression-тестами. В это ТЗ **не входит**
новый production manifest load, media backfill, cold extraction, live
Late-Fusion smoke, полный release run, новый passed bundle, commit, push, tag
или deploy.

Отдельный финальный прогон разрешается описывать и запускать только после
повторного read-only ревью с нулём P0/P1/P2.

## Фаза 1. Настоящий exact schema registry

1. Заменить `REPORT_CONTRACTS: kind -> producer/schema` на versioned contract,
   содержащий для каждого kind:
   - producer;
   - payload schema version;
   - exact required payload fields;
   - exact allowed payload fields;
   - field type/enum constraints;
   - auxiliary file contract, если он есть.
2. Общий JSON loader должен reject duplicate keys до построения dict.
3. `validate_report_envelope()` обязан отклонять:
   - missing required payload fields;
   - unknown payload fields;
   - wrong types, invalid enum/status;
   - envelope/source-input/runtime/payload-SHA mismatch.
4. Builder и verifier используют один validator; kind-specific semantic
   проверки не должны расходиться.
5. `stamp_report()` для release producer требует явные producer, schema,
   source inputs и argv без release-дефолтов.

Acceptance:

- пустой security/tests/smoke/cold report отклоняется;
- unknown ordinary payload field отклоняется;
- JSON с duplicate key отклоняется;
- все девять штатных producer fixtures проходят одним validator-ом.

## Фаза 2. Закрыть file/path trust boundary builder-а

1. Builder получает явный `--staging-root`; все report/log/summary/ledger
   sources обязаны после `resolve()` находиться внутри него.
2. Запретить symlink/reparse point, path traversal, device/non-regular files и
   source/output overlap.
3. Test auxiliary contract фиксированный:
   - pytest -> `logs/pytest.log`;
   - ruff -> `logs/ruff.log`;
   - compileall -> `logs/compileall.log`;
   - diff_check -> `logs/diff_check.log`;
   - summary -> `logs/test_summary.json`.
   Путь нельзя строить из report-controlled имени.
4. Smoke ledger имеет единственный фиксированный destination.
5. До первого copy провалидировать exact check names, source containment,
   declared bytes/SHA и отсутствие collisions.
6. Копировать streaming, затем повторно сверять bytes/SHA; не использовать
   `read_bytes()` для потенциально больших логов.

Acceptance:

- `../../escaped`, absolute path, symlink и внешний log source отклоняются до
  записи любого файла;
- fault test доказывает отсутствие escaped residue;
- builder не может упаковать `.env` или произвольный внешний файл через
  forged receipt.

## Фаза 3. Exact physical bundle и публикация builder-а

1. `_evidence_bundle()` строит recursive physical inventory и требует точного
   равенства:
   `declared paths + evidence_manifest.json == files on disk`.
2. Разрешённый auxiliary kind set должен быть точным, а не prefix-based.
3. Self-preflight выполнять на temp directory **до** финального rename.
4. После успешного preflight: fsync всех файлов и каталогов, atomic rename,
   fsync parent.
5. Ошибка до rename удаляет только temp и публикует отдельный минимальный
   immutable failed-builder artifact; уже опубликованный attempt не удалять.
6. Collision/parallel writers закрыть lock/exclusive create и уникальным temp.

Acceptance:

- unlisted file, missing declared file, extra log kind и byte tamper блокируют
  preflight;
- final bundle никогда не появляется до успешного полного preflight;
- failed attempt evidence сохраняется, passed/truncated output не остаётся.

## Фаза 4. Достоверный и base-aware test runner

1. Runner сам задаёт четыре exact argv; verifier сравнивает list equality, а
   не token substrings.
2. Добавить обязательный `--base-ref` и сохранить:
   - remote/base ref;
   - base commit;
   - merge-base SHA;
   - HEAD SHA.
3. Diff command: exact `git diff --check <merge-base>..HEAD --`; plain
   working-tree `git diff --check` release-доказательством не считается.
4. Для каждого subprocess задать именованный timeout; timeout создаёт failed
   receipt с stdout/stderr и ненулевым gate.
5. Logs писать durable atomic writer-ом в новый attempt staging; запретить
   overwrite существующих receipts.
6. Summary разделить на:
   - `gates_passed/gates_failed`;
   - pytest parsed counts или отдельный JUnit/JSON receipt с SHA.
   Поле `passed` не должно смешивать команды и тесты.
7. Release `build_report()` не должен принимать caller-supplied произвольные
   receipts/exit codes; stamped report строится только из результата runner-а.

Acceptance:

- `echo pytest`, reordered/extra argv и wrong executable отклоняются;
- commit с whitespace error относительно base блокируется на чистом tree;
- timeout/interrupt сохраняет failed log/receipt;
- summary нельзя вручную рассогласовать с runner outputs.

## Фаза 5. Строгий smoke ledger и case evidence

1. Последняя attempt обязана совпадать с top-level report:
   - same attempt_id;
   - `passed=true`;
   - `case_count=10`;
   - `errors=[]`;
   - cases digest/count совпадает.
2. Attempt IDs канонические, уникальные и монотонные; hash-chain покрывает всю
   историю.
3. Для каждого case проверить:
   - exact unique case ID set;
   - `citation_count == len(unique citation_ids)`;
   - `reference_count == len(references)`;
   - unique valid reference IDs;
   - citations subset references;
   - `youtube_start_url_count` равен фактическому числу валидных start URLs;
   - URL и start URL sanitized http(s);
   - pipeline Late-Fusion, fallback false, errors empty.
4. Bundled ledger byte-for-byte равен stamped attempts и заявлен в exact
   bundle inventory.

Acceptance:

- валидно перехешированная failed last attempt блокирует smoke;
- count/list mismatch, duplicate reference ID и invalid start URL блокируются;
- предыдущая failed attempt сохраняется после успешного retry.

## Фаза 6. Pure accepted-manifest validator

1. Вынести из `loader/ingest.py` pure read-only validator без записи load
   report и без LightRAG mutation.
2. Его обязаны вызывать loader, bundle builder, cold source proof и final
   verifier.
3. Проверять exact root schema, pipeline identity, allowed roots, containment,
   symlinks/reparse points, unique source/path/doc IDs, SHA types/bytes,
   provenance equality, graph_text и blocked flags.
4. `cold_provenance._resolve_manifest_file()` не должен принимать файл вне
   разрешённых normalized/enriched roots.

Acceptance:

- forged `accepted_manifest_v2` с path вне corpus roots отклоняется до чтения
  содержимого и до load;
- producer/loader/verifier получают одинаковый список deterministic errors.

## Фаза 7. Exact current-state reconciliation

1. Provenance producer сохраняет deterministic state snapshot SHA минимум для:
   - doc status/full docs;
   - `doc_metadata_index.sqlite`;
   - relevant GraphML/LightRAG stores;
   - FTS и source registry SQLite.
2. Verifier повторно запускает current provenance resolution по каждому
   expected doc, а не доверяет сохранённым source/doc lists.
3. Проверять exact metadata-index aliases/paths/source IDs и отсутствие лишних
   rows.
4. FTS/registry gate сравнивает exact expected row sets и identity fields, а
   counts использует только как дополнительную метрику.
5. Текущий snapshot SHA должен точно совпасть с producer snapshot SHA.
6. Для SQLite определить стабильный read protocol: отсутствие активного
   writer/WAL residue либо read-only backup snapshot + integrity check.
7. `state_inventory` включает metadata index и все state-affecting stores;
   SHA вычисляются streaming.

Acceptance:

- замена строки при том же count блокируется;
- изменение metadata index/FTS/registry после producer report блокируется;
- stale/extra alias и неразрешимый provenance path блокируют verdict;
- unchanged exact state проходит повторную проверку.

## Фаза 8. Атомарный final verdict

1. Проверить canonical run directory и exclusive ownership до тяжёлой работы.
2. В temp directory создать final JSON, Markdown и
   `verification_manifest.json`; manifest хеширует уже готовые JSON/Markdown.
3. Выполнить полный self-check verification manifest в temp.
4. Fsync файлы/каталоги, затем один atomic directory rename и parent fsync.
5. Ошибка рендера, manifest write, fsync или rename должна создавать отдельный
   читаемый FAILED artifact без появления partial final directory.
6. Никаких записей внутрь final directory после rename.

Acceptance:

- fault injection на JSON/Markdown/manifest/fsync/rename не оставляет final
  directory без полного manifest;
- повторный writer не перезаписывает attempt;
- FAILED artifact всегда JSON-readable и имеет ненулевой exit code.

## Фаза 9. Regression suite и повторное ревью

Добавить negative tests на каждый reproduction из review:

- report payload missing/unknown/duplicate fields;
- external/symlink/traversal auxiliary source;
- unlisted physical bundle file;
- fake/reordered test argv и non-base diff;
- timeout/interrupt receipt;
- failed last smoke attempt и count mismatch;
- manifest path outside allowed roots;
- changed metadata index/FTS/registry with same counts;
- verification manifest failure after JSON/Markdown preparation;
- builder preflight failure without residue/deletion of published evidence.

Запустить только:

- затронутые fixture/unit tests;
- targeted Ruff;
- py_compile затронутых модулей;
- `git diff --check HEAD` для текущей статической работы.

Запрещены полный pytest, production/cold/live run и новый passed release bundle.

## Статический DoD

- Все 8 P1 и 2 P2 из R2 review закрыты кодом и negative tests.
- Builder не читает и не пишет вне явно разрешённых roots.
- Bundle physical inventory точный и проверен до публикации.
- Test receipts доказывают exact base-aware команды.
- Last smoke attempt и top-level evidence не могут расходиться.
- Manifest/metadata/FTS/registry проверяются exact sets и свежими SHA.
- Final verdict появляется только полностью и атомарно.
- Повторное read-only ревью находит **0 P0/P1/P2**.

Только после этого можно вернуть статус `READY_FOR_FINAL_RUN` и отдельно
актуализировать execution-ТЗ. До этого существующее final execution-ТЗ
заблокировано и не запускается.
