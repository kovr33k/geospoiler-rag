"""Compatibility entry point for geospoiler.ingestion.normalizer.instagram.vision."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.normalizer.instagram.vision')
