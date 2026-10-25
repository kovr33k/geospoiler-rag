"""Compatibility entry point for geospoiler.answering.late_fusion."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.answering.late_fusion')
