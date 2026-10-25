"""Compatibility entry point for geospoiler.answering.answer_postprocess."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.answering.answer_postprocess')
