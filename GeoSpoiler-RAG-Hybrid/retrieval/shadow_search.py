"""Compatibility entry point for geospoiler.retrieval.cards.shadow_search."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.retrieval.cards.shadow_search')
