"""Compatibility entry point for geospoiler.operations.smoke_contract."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.operations.smoke_contract')
