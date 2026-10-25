"""Compatibility entry point for geospoiler.operations.cold_graph_check."""

import importlib
import sys

if __name__ == "__main__":
    from geospoiler.operations.cold_graph_check import main
    from geospoiler.operations.release_identity import run_release_cli

    raise SystemExit(run_release_cli(main, component='cold_graph_check'))
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.operations.cold_graph_check')
