"""Compatibility entry point for geospoiler.ingestion.enricher.triage."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.enricher.triage')
