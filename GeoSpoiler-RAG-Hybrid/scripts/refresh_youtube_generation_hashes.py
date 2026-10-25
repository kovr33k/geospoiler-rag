"""Compatibility entry point for geospoiler.operations.refresh_youtube_generation_hashes."""

import importlib
import sys

if __name__ == "__main__":
    import runpy

    runpy.run_module('geospoiler.operations.refresh_youtube_generation_hashes', run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.operations.refresh_youtube_generation_hashes')
