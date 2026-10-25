"""Compatibility entry point for geospoiler.ingestion.enricher.youtube_segmenter."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.enricher.youtube_segmenter')
