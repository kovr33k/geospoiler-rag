"""Compatibility entry point for geospoiler.operations.late_fusion_activation_smoke."""

import importlib
import sys

if __name__ == "__main__":
    from geospoiler.operations.late_fusion_activation_smoke import main
    from geospoiler.operations.release_identity import run_release_cli

    raise SystemExit(run_release_cli(main, component='late_fusion_activation_smoke'))
else:
    sys.modules[__name__] = importlib.import_module('geospoiler.operations.late_fusion_activation_smoke')
