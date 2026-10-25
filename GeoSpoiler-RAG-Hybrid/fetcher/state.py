"""Compatibility entry point for geospoiler.ingestion.fetcher.state."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.fetcher.state')
