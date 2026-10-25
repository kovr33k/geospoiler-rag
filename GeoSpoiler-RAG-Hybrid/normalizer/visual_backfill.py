"""Compatibility entry point for geospoiler.ingestion.normalizer.visual_backfill."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.normalizer.visual_backfill')
