"""Compatibility entry point for geospoiler.operations.cold_provenance."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.operations.cold_provenance')
