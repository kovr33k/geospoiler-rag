"""Compatibility entry point for geospoiler.testing_support.no_network."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.testing_support.no_network')
