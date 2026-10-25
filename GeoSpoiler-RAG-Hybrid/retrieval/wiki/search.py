"""Compatibility entry point for geospoiler.wiki.search."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.wiki.search')
