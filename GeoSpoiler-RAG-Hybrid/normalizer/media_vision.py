"""Compatibility entry point for geospoiler.ingestion.normalizer.media_vision."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.normalizer.media_vision')
