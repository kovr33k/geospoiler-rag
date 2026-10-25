"""Compatibility entry point for geospoiler.ingestion.normalizer.image_handler."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.normalizer.image_handler')
