"""Compatibility entry point for geospoiler.config."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.config')
