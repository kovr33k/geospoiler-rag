"""Compatibility entry point for geospoiler.providers.clients."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.providers.clients')
