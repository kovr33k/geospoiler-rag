"""Compatibility entry point for geospoiler.ui.reviewer_app."""

import importlib
import sys

if __name__ == "__main__":
    import runpy

    runpy.run_module('geospoiler.ui.reviewer_app', run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.ui.reviewer_app')
