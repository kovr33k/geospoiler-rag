"""Compatibility entry point for geospoiler.providers.runtime."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.providers.runtime')
