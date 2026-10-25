"""Compatibility entry point for geospoiler.wiki.schema."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.wiki.schema')
