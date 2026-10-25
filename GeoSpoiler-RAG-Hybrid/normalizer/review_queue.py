"""Compatibility entry point for geospoiler.ingestion.normalizer.review_queue."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.normalizer.review_queue')
