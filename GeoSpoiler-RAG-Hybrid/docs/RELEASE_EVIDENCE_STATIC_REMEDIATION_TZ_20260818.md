# ТЗ: статическое закрытие release-контура

Статус: **SUPERSEDED — BLOCKED BY R2 REVIEW**. Статическое remediation первой
итерации не закрыло все negative paths. Актуальные findings и исправления:
`RELEASE_EVIDENCE_STATIC_REVIEW_R2_20260818.md` и
`RELEASE_EVIDENCE_STATIC_FIX_TZ_R2_20260818.md`.

Цель — закрыть evidence-контракты до единственного отдельно разрешаемого
финального release/cold/live прогона. До статического DoD запрещены production
load, cold extraction, Late-Fusion smoke, media backfill, полный pytest и
создание нового passed bundle.

## Обязательные исправления

1. Release builder fail-closed на dirty tracked/untracked tree. Отчёт нельзя
   перепривязать к новой identity после выполнения команды.
2. Каждый producer выдаёт единый envelope: producer,
   producer_schema_version, generated_at, effective_runtime_identity,
   source_inputs, command_argv, payload_sha256. Builder и verifier проверяют
   exact schema и payload SHA.
3. Cold effective runtime (batch size/timeout, RAG timeouts, config identity)
   входит в checkpoint и report. Скрытый override, не совпадающий с identity,
   блокирует verifier. Сохраняются workspace inventory и lineage:
   inserted_by_loader, reused_processed, processed_terminal.
4. Manifest-load связывается только по run_id и SHA, не по basename.
   Producer/verifier используют одну схему; ручные aliases запрещены.
5. Добавляется test-evidence producer с SHA и byte count четырёх raw logs и
   отдельными exit codes для pytest, Ruff, compileall и base-aware diff-check.
   Отсутствующий exit code не считается нулём.
6. Smoke хранит attempt_id и immutable attempts[]; retry не скрывает
   предыдущую ошибку.
7. Bundle и verifier output публикуются через temp sibling + fsync + atomic
   rename. Для одного run используются attempt-001, attempt-002, а не
   неоднозначные final, final2.
8. Verifier сохраняет deterministic SHA-inventory normalized/enriched,
   LightRAG JSON и FTS/registry SQLite; counts без inventory недостаточны.
9. Исторические v4/v5 документы помечаются как non-verdict. Token rotation
   считается закрытой пользовательским подтверждением.

## Статический DoD

- exact producer→builder→verifier schemas согласованы;
- dirty tree, post-hoc restamp, hidden cold override и неполный tests report
  fail-closed;
- cold lineage/workspace inventory и независимые digests проверяются;
- smoke retry history не теряется;
- atomic publication и canonical run index реализованы;
- локальные fixture/unit/static checks зелёные;
- повторное статическое ревью не находит P0/P1/P2.

После этого статус меняется только на READY_FOR_FINAL_RUN. Новый release run,
новый cold/live smoke, full suite и итоговый READY_FOR_RELEASE оформляются
отдельным execution-ТЗ.

## Результат первой реализации

- Изменения producer→builder→verifier внесены и покрыты локальными
  regression-тестами, но это не закрыло весь статический DoD.
- Проверено: `114 passed, 2 warnings, 3 subtests passed`; Ruff, compileall и
  `git diff --check` зелёные.
- Проверены отдельные fail-closed фикстуры для dirty tree, payload SHA,
  post-hoc manifest/run-id, cold override/lineage и неполного test-evidence.
- Повторное ревью выявило, что exact schemas, независимые cold digests,
  обязательный run index, smoke attempt ledger, freshness текущего состояния и
  verifier error-path ещё не закрыты. Исправления описаны в
  `docs/RELEASE_EVIDENCE_REVIEW_FIX_TZ_20260818.md`.
- Production load, cold extraction, media backfill, live smoke, full suite,
  новый bundle и push не выполнялись. Финальный run остаётся запрещённым до
  полного выполнения follow-up ТЗ.

## Финальный статический результат

Follow-up ТЗ из `RELEASE_EVIDENCE_REVIEW_FIX_TZ_20260818.md` выполнено.
Producer/builder/verifier используют общий schema registry; canonical bundle,
runner receipts, independent cold proofs, smoke hash-chain, freshness gates,
atomic failure-path и negative tests реализованы. Последняя локальная проверка:
83 passed, 2 skipped, Ruff/py_compile/diff-check passed. Состояние означает
готовность к отдельному финальному прогону, но не является его результатом и
не даёт `READY_FOR_RELEASE` до получения production/cold/live evidence.
