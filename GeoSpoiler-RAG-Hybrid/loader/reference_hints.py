"""Compatibility entry point for geospoiler.answering.reference_hints."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.answering.reference_hints')
