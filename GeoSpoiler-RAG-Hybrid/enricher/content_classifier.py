"""Compatibility entry point for geospoiler.ingestion.enricher.content_classifier."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.enricher.content_classifier')
