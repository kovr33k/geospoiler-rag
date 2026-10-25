"""Compatibility entry point for geospoiler.answering.composer."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.answering.composer')
