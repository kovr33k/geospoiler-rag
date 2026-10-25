"""Compatibility entry point for geospoiler.cli.auth."""

import importlib
import sys

if __name__ == "__main__":
    import runpy

    runpy.run_module('geospoiler.cli.auth', run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.cli.auth')
