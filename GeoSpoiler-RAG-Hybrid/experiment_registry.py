"""Compatibility entry point for geospoiler.evaluation.experiment_registry."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.evaluation.experiment_registry')
