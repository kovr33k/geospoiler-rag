"""Compatibility entry point for geospoiler.retrieval.lightrag.entity_merge."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.retrieval.lightrag.entity_merge')
