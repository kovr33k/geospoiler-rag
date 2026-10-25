"""Compatibility wrapper for secondary CLI tool commands.

New code should import from ``cli_tools``. This module remains so existing
scripts that import ``cli`` keep working during the CLI split.
"""

from geospoiler.cli.tools import (
    cmd_experiments_index,
    cmd_fts_rebuild,
    cmd_fts_search,
    cmd_registry_rebuild,
    cmd_registry_resolve,
    cmd_transcribe_backfill,
    cmd_validate_enriched,
)

__all__ = [
    "cmd_experiments_index",
    "cmd_fts_rebuild",
    "cmd_fts_search",
    "cmd_registry_rebuild",
    "cmd_registry_resolve",
    "cmd_transcribe_backfill",
    "cmd_validate_enriched",
]
