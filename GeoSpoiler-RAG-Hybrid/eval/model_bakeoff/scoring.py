"""Compatibility entry point for geospoiler.evaluation.model_bakeoff.scoring."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.evaluation.model_bakeoff.scoring')
