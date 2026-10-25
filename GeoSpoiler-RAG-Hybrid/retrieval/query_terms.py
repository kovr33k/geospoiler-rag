"""Compatibility entry point for geospoiler.retrieval.cards.query_terms."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.retrieval.cards.query_terms')
