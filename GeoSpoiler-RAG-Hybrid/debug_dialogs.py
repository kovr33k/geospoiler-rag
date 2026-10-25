"""Compatibility entry point for geospoiler.cli.debug_dialogs."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.cli.debug_dialogs')
