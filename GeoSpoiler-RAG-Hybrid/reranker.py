"""Compatibility entry point for geospoiler.providers.reranker."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.providers.reranker')
