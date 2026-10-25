"""Compatibility entry point for geospoiler.ingestion.fetcher.telegram_client."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.fetcher.telegram_client')
