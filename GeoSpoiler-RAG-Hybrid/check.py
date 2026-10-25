"""Compatibility entry point for geospoiler.cli.check."""

import importlib
import sys

if __name__ == "__main__":
    import runpy

    runpy.run_module('geospoiler.cli.check', run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.cli.check')
