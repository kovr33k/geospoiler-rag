"""Compatibility entry point for geospoiler.cli.response_formatter."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.cli.response_formatter')
