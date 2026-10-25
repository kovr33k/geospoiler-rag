"""Compatibility entry point for geospoiler.ingestion.enricher.graph_text_builder."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.enricher.graph_text_builder')
