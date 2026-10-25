"""Compatibility entry point for geospoiler.wiki.state."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.wiki.state')
