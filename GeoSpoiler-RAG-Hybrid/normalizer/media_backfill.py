"""Compatibility entry point for geospoiler.ingestion.normalizer.media_backfill."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.normalizer.media_backfill')
