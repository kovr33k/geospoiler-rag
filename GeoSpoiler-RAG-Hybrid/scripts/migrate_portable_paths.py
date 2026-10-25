"""Compatibility entry point for geospoiler.operations.migrate_portable_paths."""

import importlib
import sys

if __name__ == "__main__":
    from geospoiler.operations.migrate_portable_paths import main
    from geospoiler.operations.release_identity import run_release_cli

    raise SystemExit(run_release_cli(main, component='migrate_portable_paths'))
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.operations.migrate_portable_paths')
