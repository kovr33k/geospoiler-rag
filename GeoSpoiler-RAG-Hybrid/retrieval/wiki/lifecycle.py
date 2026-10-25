"""Compatibility entry point for geospoiler.wiki.lifecycle."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.wiki.lifecycle')
