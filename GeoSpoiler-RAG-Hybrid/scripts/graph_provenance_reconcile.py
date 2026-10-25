"""Compatibility entry point for geospoiler.operations.graph_provenance_reconcile."""

import importlib
import sys

if __name__ == "__main__":
    from geospoiler.operations.graph_provenance_reconcile import main
    from geospoiler.operations.release_identity import run_release_cli

    raise SystemExit(run_release_cli(main, component='graph_provenance_reconcile'))
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.operations.graph_provenance_reconcile')
