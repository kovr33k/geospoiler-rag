"""Compatibility entry point for geospoiler.retrieval.cards.card_text."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.retrieval.cards.card_text')
