"""Compatibility entry point for geospoiler.answering.profiles."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.answering.profiles')
