"""Compatibility entry point for geospoiler.wiki.eligibility."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.wiki.eligibility')
