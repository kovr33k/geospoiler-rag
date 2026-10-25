"""Compatibility entry point for geospoiler.wiki.projections."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.wiki.projections')
