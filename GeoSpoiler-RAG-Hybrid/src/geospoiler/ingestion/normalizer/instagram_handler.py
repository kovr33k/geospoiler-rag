"""Compatibility alias preserving Instagram imports and helper patches."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.normalizer.instagram.pipeline')
