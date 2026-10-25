"""Compatibility entry point for geospoiler.retrieval.cards.card_fts."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.retrieval.cards.card_fts')
