"""Compatibility entry point for geospoiler.operations.release_identity."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.operations.release_identity')
