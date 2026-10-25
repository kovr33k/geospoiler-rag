"""Compatibility entry point for geospoiler.wiki.relations."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.wiki.relations')
