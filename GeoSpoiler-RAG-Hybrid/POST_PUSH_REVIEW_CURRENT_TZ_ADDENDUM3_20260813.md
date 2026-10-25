# Addendum 3 к ТЗ текущего review: graph provenance

## P1 — deterministic LightRAG provenance reconciliation

Fresh data review выявил 129 `doc_status` records, среди которых 52 обычных basename paths, 2 YouTube basename paths и 75 virtual paths. `resolve_source_path` запрещает небезопасный basename lookup, поэтому обычные provenance могут не разрешаться детерминированно, хотя metadata index содержит canonical/virtual rows.

Требуется:

1. Провести deterministic reconciliation всех 129 `source_id`/`normalized_path`/`card_path` против LightRAG `doc_status`, metadata index и accepted manifest.
2. Для basename records либо создать явный canonical alias в индексе, либо мигрировать записи на virtual canonical names. Запрещён fallback по одному basename.
3. Добавить regression test на duplicate basename в разных каналах, Windows separators, missing file, stale path и virtual YouTube path.
4. Сформировать machine report `matched/missing/ambiguous/stale` и не считать graph acceptance зелёной при любом `ambiguous` или `missing`.
5. После reconciliation повторить graph/FTS/registry counts и SHA baseline; accepted manifest loader должен принимать только exact canonical provenance.

Gate: 129/129 deterministic matches, `ambiguous=0`, `missing=0`, stale China/6 и China/72 = 0, затем новый immutable final verification report.
