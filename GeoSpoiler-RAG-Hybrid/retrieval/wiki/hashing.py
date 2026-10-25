"""Compatibility entry point for geospoiler.wiki.hashing."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.wiki.hashing')
