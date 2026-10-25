"""Shared LightRAG runtime state."""

import contextvars
import logging

logger = logging.getLogger("geospoiler.loader")
LLM_ROLE = contextvars.ContextVar("geospoiler_lightrag_llm_role", default="query")
