"""Compatibility entry point for geospoiler.answering.card_context."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.answering.card_context')
