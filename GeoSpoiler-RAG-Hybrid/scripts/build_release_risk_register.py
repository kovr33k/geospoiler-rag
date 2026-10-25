"""Compatibility entry point for geospoiler.operations.build_release_risk_register."""

import importlib
import sys

if __name__ == "__main__":
    from geospoiler.operations.build_release_risk_register import main
    from geospoiler.operations.release_identity import run_release_cli

    raise SystemExit(run_release_cli(main, component='build_release_risk_register'))
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.operations.build_release_risk_register')
