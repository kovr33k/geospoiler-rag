"""Compatibility entry point for geospoiler.sources.registry."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.sources.registry')
