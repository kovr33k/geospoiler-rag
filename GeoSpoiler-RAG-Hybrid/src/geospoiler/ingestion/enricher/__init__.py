"""
Enricher Module v2 — transforms normalized posts into structured memory cards.

Pipeline: normalized/ -> preprocessor -> classifier -> LLM -> validator -> repair -> postprocessor -> enriched/

Each normalized post gets an enriched_v2 JSON card containing:
- summary, key_points, entities (raw surface forms), topics, theses, quotes, events, search_phrases
- content_type classification (rule-based, not LLM)
- graph_text (code-built relational text for LightRAG)
- search_text (code-built dense text for FTS/BM25)
- source_chain (code-built from metadata)
- ignored_blocks (preprocessor-detected)
- quality_flags, provenance
"""
