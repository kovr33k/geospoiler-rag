"""Compatibility entry point for geospoiler.compat.lightrag_loader."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.compat.lightrag_loader')
