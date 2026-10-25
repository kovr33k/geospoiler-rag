"""Compatibility entry point for geospoiler.operations.sync_media_contract."""

import importlib
import sys

if __name__ == "__main__":
    from geospoiler.operations.release_identity import run_release_cli
    from geospoiler.operations.sync_media_contract import main

    raise SystemExit(run_release_cli(main, component='sync_media_contract'))
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.operations.sync_media_contract')
