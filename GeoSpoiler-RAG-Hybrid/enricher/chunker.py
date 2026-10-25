"""Compatibility entry point for geospoiler.ingestion.enricher.chunker."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.enricher.chunker')
