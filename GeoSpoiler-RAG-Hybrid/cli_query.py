"""Compatibility entry point for geospoiler.cli.query."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.cli.query')
