"""Compatibility entry point for geospoiler.ingestion.enricher.preprocessor."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.enricher.preprocessor')
