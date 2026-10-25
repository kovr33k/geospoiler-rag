"""Compatibility entry point for geospoiler.wiki.sidecars."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.wiki.sidecars')
