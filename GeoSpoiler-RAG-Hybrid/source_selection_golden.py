"""Compatibility entry point for geospoiler.evaluation.source_selection_golden."""

import importlib
import sys

if __name__ == "__main__":
    import runpy

    runpy.run_module('geospoiler.evaluation.source_selection_golden', run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.evaluation.source_selection_golden')
