"""Compatibility entry point for geospoiler.ui.wiki_reviewer."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ui.wiki_reviewer')
