# Execution-ТЗ: единственный финальный release/cold/live прогон

Статус: **BLOCKED — DO NOT RUN**. Повторное статическое ревью R2 нашло
остаточные release blockers. Сначала полностью выполнить
`RELEASE_EVIDENCE_STATIC_FIX_TZ_R2_20260818.md` и получить повторное ревью с
нулём P0/P1/P2. Этот документ до тех пор не является разрешением на запуск.

## Предусловия

- Рабочее дерево чистое, commit/tree SHA зафиксированы; запускать только из
  того же checkout и `.venv`, который войдёт в release identity.
- Accepted manifest имеет `accepted_manifest_v2`, собственный `run_id`, SHA
  normalized/enriched/graph_text для каждой записи и не содержит удалённых
  источников.
- `LATE_FUSION_ENABLED=true`, `RERANKER_ENABLED=false`, активный Luna runtime;
  runtime identity и manifest SHA сохраняются до начала всех producer-ов.
- User-attested `RISK-TOKEN-ROTATION-001` остаётся `closed` без секретного
  значения в evidence.

## Порядок выполнения

1. Создать внешний staging каталога `attempt-NNN`; не переиспользовать каталог
   предыдущей попытки.
2. Выполнить runner test-gate: pytest, Ruff, compileall и `git diff --check`.
   Producer обязан сохранить receipts, raw logs и summary; ручные exit-коды
   запрещены.
3. Сгенерировать свежие manifest-load, provenance, security, backup,
   portable-paths, cold-graph, Late-Fusion smoke и risk reports с одним
   release `run_id`. Для smoke запускать только штатный 10-case набор; ledger
   должен быть выведен из каталога попытки.
4. Перед cold-load выполнить media/card contract gate и, если он не равен
   нулю, остановить публикацию и сначала завершить штатный re-enrichment /
   media-sync. Caption-only результат с failed media без pipeline-owned flag
   или extraction issue запрещён.
5. Запустить accepted manifest load в чистый изолированный LightRAG workspace;
   затем cold runner с независимыми SHA normalized/enriched/graph_text,
   loaded full-doc content, exact source/doc sets, lineage и storage inventory.
6. Выполнить свежую provenance reconciliation без basename aliases, затем
   exact FTS/registry/URL coverage и repository-wide security scan.
7. Собрать bundle builder-ом. Builder должен пройти собственный preflight;
   verifier должен читать только bundle, проверить все SHA/bytes/receipts,
   current-state freshness и опубликовать `verification_manifest.json`.

## Release gate

`READY_FOR_RELEASE` допускается только если одновременно passed:

- accepted manifest load: expected=inserted, skipped=failed=0, matching run/SHA;
- media/card mismatches = 0 и технические flags/issues синхронизированы;
- cold graph: exact 100% source/doc sets, all processed, no copied storage,
  independent loaded-content hashes, no ambiguous lineage;
- provenance: 0 missing/ambiguous/nonterminal и exact manifest source/doc sets;
- FTS/registry: card/source counts соответствуют текущему corpus, YouTube/Wiki
  counters ненулевые, `references_with_urls == references`;
- security scan: 0 findings/occurrences/unreadable/protected/git errors;
- smoke: 10/10 Late-Fusion, fallback=false, valid citations and sanitized
  http(s) references, complete attempt hash-chain/ledger;
- runner test-gate: все четыре команды exit code 0;
- verifier status `passed`, failed_checks пуст, JSON/Markdown SHA совпадают с
  `verification_manifest.json`.

Любой один провал означает `FAILED`, сохранение immutable evidence и остановку
без push/tag/deploy. После публикации verdict допускается отдельный user-
authorized commit/push; он не является частью этого execution-ТЗ.
