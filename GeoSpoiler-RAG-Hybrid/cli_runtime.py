"""Compatibility entry point for geospoiler.cli.runtime."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.cli.runtime')
