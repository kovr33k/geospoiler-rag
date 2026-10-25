"""Compatibility entry point for geospoiler.retrieval.lightrag.graph_quality."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.retrieval.lightrag.graph_quality')
