"""Compatibility entry point for geospoiler.ingestion.enricher.llm_enricher."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.enricher.llm_enricher')
