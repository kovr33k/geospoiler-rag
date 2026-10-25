"""Compatibility entry point for geospoiler.evaluation.baseline_probe."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.evaluation.baseline_probe')
