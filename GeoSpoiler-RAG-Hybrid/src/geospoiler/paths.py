"""Locate checkout configuration without depending on the process directory."""

import json
import os
from functools import lru_cache
from pathlib import Path


def project_root() -> Path:
    configured = os.getenv("GEOSPOILER_PROJECT_ROOT", "").strip()
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute():
            raise ValueError("GEOSPOILER_PROJECT_ROOT must be an absolute path")
        return root.resolve()
    # Source/editable installation: require the project marker, not a fixed
    # number of parents. Wheel installations must provide an explicit root.
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src/geospoiler").is_dir():
            return parent
    raise RuntimeError(
        "Set GEOSPOILER_PROJECT_ROOT to the absolute directory containing your "
        ".env and project data; installed packages do not store runtime data."
    )


# Legacy names are logical addresses used in existing cards and graph IDs.
# Their physical locations may change without rewriting the evidence corpus.
_LEGACY_PATHS = (
    ("output/normalized_youtube", "YOUTUBE_NORMALIZED_DIR"),
    ("output/enriched_segments", "YOUTUBE_SEGMENTS_DIR"),
    ("output/normalized", "NORMALIZED_DIR"),
    ("output/enriched", "ENRICHED_DIR"),
    ("output/review_queue", "REVIEW_QUEUE_DIR"),
    ("output/transcripts", "TRANSCRIPTION_DIR"),
    ("output/visuals", "MEDIA_VISUAL_DIR"),
    ("output/instagram_cache", "INSTAGRAM_CACHE_DIR"),
    ("output/wiki", "WIKI_OUTPUT_DIR"),
    ("artifacts/card_fts.sqlite", "CARD_FTS_DB_PATH"),
    ("artifacts/source_registry.sqlite", "SOURCE_REGISTRY_DB_PATH"),
    ("artifacts/wiki_state.sqlite", "WIKI_STATE_DB_PATH"),
    ("wiki_sidecars", "WIKI_SIDECAR_DIR"),
    ("rag_storage", "RAG_STORAGE_DIR"),
    ("media_cache", "MEDIA_CACHE_DIR"),
    ("state", "STATE_DIR"),
)


@lru_cache(maxsize=8)
def _read_layout(marker: str, modified_ns: int) -> dict:
    data = json.loads(Path(marker).read_text(encoding="utf-8"))
    if data.get("version") != 2 or not Path(data.get("identity_root", "")).is_absolute():
        raise ValueError("Invalid data layout marker")
    return data


def _layout():
    from geospoiler import config

    marker = config.PROJECT_ROOT / "data/.layout-v2.json"
    if getattr(config, "DATA_LAYOUT_VERSION", 1) != 2 or not marker.is_file():
        return config, None
    return config, _read_layout(str(marker), marker.stat().st_mtime_ns)


def resolve_project_path(value: str | Path) -> Path:
    """Resolve persisted old/new addresses to the active physical data tree."""
    config, layout = _layout()
    path = Path(value)
    if not path.is_absolute():
        path = config.PROJECT_ROOT / path
    if layout is None:
        return path
    # Resolve traversal before choosing an alias; consumers still enforce their
    # own allowed-root and symlink checks on the returned physical path.
    path = Path(os.path.abspath(path))
    for root in (config.PROJECT_ROOT, Path(layout["identity_root"])):
        for legacy, key in _LEGACY_PATHS:
            target = Path(getattr(config, key))
            if not target.is_relative_to(config.PROJECT_ROOT / "data"):
                continue  # Explicit external roots own their own identities.
            try:
                suffix = path.relative_to(root / legacy)
            except ValueError:
                continue
            return target / suffix
    return path


def logical_project_path(value: str | Path) -> Path:
    """Keep pre-migration absolute document identities for moved materials."""
    config, layout = _layout()
    if layout is None:
        return Path(value).resolve(strict=False)
    path = resolve_project_path(value).resolve(strict=False)
    for legacy, key in _LEGACY_PATHS:
        target = Path(getattr(config, key)).resolve(strict=False)
        if not target.is_relative_to(config.PROJECT_ROOT / "data"):
            continue
        try:
            suffix = path.relative_to(target)
        except ValueError:
            continue
        return Path(layout["identity_root"]) / legacy / suffix
    return path


def persisted_identity_path(value: str | Path) -> str:
    """Preserve fingerprint spelling until an explicitly migrated layout exists."""
    _, layout = _layout()
    return str(logical_project_path(value)) if layout is not None and value else str(value)
