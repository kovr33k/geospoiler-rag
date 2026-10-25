"""Compatibility entry point for geospoiler.evaluation.model_bakeoff.select_china_posts."""

import importlib
import sys

if __name__ == "__main__":
    import runpy

    runpy.run_module('geospoiler.evaluation.model_bakeoff.select_china_posts', run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.evaluation.model_bakeoff.select_china_posts')
