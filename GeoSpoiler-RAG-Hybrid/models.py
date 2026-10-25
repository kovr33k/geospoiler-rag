"""Compatibility entry point for geospoiler.contracts.models."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.contracts.models')
