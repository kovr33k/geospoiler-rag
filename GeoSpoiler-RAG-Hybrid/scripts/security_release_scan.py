"""Compatibility entry point for geospoiler.operations.security_release_scan."""

import importlib
import sys

if __name__ == "__main__":
    from geospoiler.operations.release_identity import run_release_cli
    from geospoiler.operations.security_release_scan import main

    raise SystemExit(run_release_cli(main, component='security_release_scan'))
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.operations.security_release_scan')
