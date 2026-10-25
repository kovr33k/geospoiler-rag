"""Compatibility entry point for geospoiler.wiki.hierarchy."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.wiki.hierarchy')
