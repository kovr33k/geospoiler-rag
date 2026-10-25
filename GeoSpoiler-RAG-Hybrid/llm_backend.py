"""Compatibility entry point for geospoiler.providers.llm_backend."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.providers.llm_backend')
