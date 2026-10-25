# Исполнение статического TZ R2

Дата: 2026-08-18  
Статус: **IMPLEMENTED — READY FOR INDEPENDENT STATIC RE-REVIEW**

## Выполнено

- Добавлен общий fail-closed validator для accepted manifests и подключён к
  loader, cold proof, bundle builder и release verifier.
- Ужесточён report envelope: exact required/allowed fields, типы, enum,
  duplicate-key rejection, producer/schema/source-input/command stamp и SHA.
- Ограничены staging roots, symlink/traversal и auxiliary-файлы; bundle
  строится через lock, streaming copy, self-preflight, temp-directory и
  atomic rename с отдельным failure artifact.
- Test evidence теперь формируется только runner-ом из точных argv,
  merge-base/HEAD, timeout-aware receipts и durable logs.
- Late-Fusion smoke получил проверку latest attempt, cases digest и hash chain.
- Добавлена read-only сверка текущего provenance/state snapshot с FTS,
  registry и metadata index; metadata index получил безопасные basename aliases
  без неоднозначного разрешения.
- Добавлены regression tests для schema/trust-boundary/inventory/smoke/
  atomic-publication и исправлена совместимость публичного metadata index.

## Проверки

Команды выполнялись в checkout `D:\\RAG-GeoSpoiler\\GeoSpoiler-RAG-Hybrid`.

- Целевой pytest: **133 passed, 1 skipped, 3 subtests passed**.
- Ruff по изменённому контуру: **All checks passed**.
- `py_compile` изменённых Python-файлов: **passed**.
- `git diff --check HEAD --`: **passed**; только штатные предупреждения о
  нормализации CRLF в двух ранее изменённых файлах.

## Ограничения

В этой фазе намеренно не запускались production manifest load, media
backfill, cold extraction, live Late-Fusion smoke, полный release run,
commit/push/deploy и любые записи в `output/` или `rag_storage/`. Перед
релизом нужен независимый read-only static re-review; только если он не найдёт
P0/P1/P2, разрешается отдельный operational run.
