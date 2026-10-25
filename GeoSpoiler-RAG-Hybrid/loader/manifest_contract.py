"""Compatibility entry point for geospoiler.retrieval.lightrag.manifest_contract."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module('geospoiler.retrieval.lightrag.manifest_contract')
