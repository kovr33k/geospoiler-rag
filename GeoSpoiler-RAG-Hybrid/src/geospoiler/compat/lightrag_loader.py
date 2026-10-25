"""Public compatibility facade for LightRAG loader operations.

The implementation lives in focused modules such as ``loader.factory``,
``loader.ingest``, ``loader.query``, and ``loader.storage``. New code should
prefer explicit imports from those owning modules; this facade preserves the
stable public loader API for older scripts.
"""

from geospoiler.answering.profiles import get_query_profile
from geospoiler.answering.query import query_rag, query_rag_result
from geospoiler.retrieval.lightrag.entity_merge import auto_fix_safe_entity_merges, plan_safe_entity_merges
from geospoiler.retrieval.lightrag.factory import create_rag
from geospoiler.retrieval.lightrag.ingest import load_from_directory, load_from_manifest, load_texts
from geospoiler.retrieval.lightrag.storage import rebuild_rag_storage

__all__ = [
    "auto_fix_safe_entity_merges",
    "create_rag",
    "get_query_profile",
    "load_from_directory",
    "load_from_manifest",
    "load_texts",
    "plan_safe_entity_merges",
    "query_rag",
    "query_rag_result",
    "rebuild_rag_storage",
]
