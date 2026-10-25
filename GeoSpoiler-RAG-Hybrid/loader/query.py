"""Compatibility entry point for geospoiler.answering.query."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.answering.query')
