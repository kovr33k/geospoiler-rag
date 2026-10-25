"""Compatibility entry point for geospoiler.ingestion.normalizer.atomic_pair."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.ingestion.normalizer.atomic_pair')
