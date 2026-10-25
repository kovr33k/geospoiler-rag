"""Compatibility entry point for geospoiler.evaluation.model_bakeoff.run_bakeoff."""

import importlib
import sys

if __name__ == "__main__":
    import runpy

    runpy.run_module('geospoiler.evaluation.model_bakeoff.run_bakeoff', run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.evaluation.model_bakeoff.run_bakeoff')
