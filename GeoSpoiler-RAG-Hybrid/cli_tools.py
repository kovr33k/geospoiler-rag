"""Compatibility entry point for geospoiler.cli.tools."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.cli.tools')
