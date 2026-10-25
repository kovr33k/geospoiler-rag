"""Compatibility entry point for geospoiler.wiki.cards."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.wiki.cards')
