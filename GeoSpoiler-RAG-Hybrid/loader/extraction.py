"""Compatibility entry point for geospoiler.retrieval.lightrag.extraction."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.retrieval.lightrag.extraction')
