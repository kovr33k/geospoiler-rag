"""Compatibility entry point for geospoiler.ingestion.enricher.pipeline."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.enricher.pipeline')
