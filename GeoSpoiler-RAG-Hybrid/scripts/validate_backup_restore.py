"""Compatibility entry point for geospoiler.operations.validate_backup_restore."""

import importlib
import sys

if __name__ == "__main__":
    from geospoiler.operations.release_identity import run_release_cli
    from geospoiler.operations.validate_backup_restore import main

    raise SystemExit(run_release_cli(main, component='validate_backup_restore'))
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.operations.validate_backup_restore')
