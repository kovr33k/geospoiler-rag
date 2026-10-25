"""Compatibility entry point for geospoiler.operations.luna_reenrich_audit."""

import importlib
import sys

if __name__ == "__main__":
    from geospoiler.operations.luna_reenrich_audit import main
    from geospoiler.operations.release_identity import run_release_cli

    raise SystemExit(run_release_cli(main, component='luna_reenrich_audit'))
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.operations.luna_reenrich_audit')
