"""Portable representation and resolution helpers for captured media paths."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

from geospoiler import config


class MediaPathError(ValueError):
    """A persisted media path escaped the configured media-cache boundary."""


def _contains_symlink(root: Path, candidate: Path) -> bool:
    current = root
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def resolve_media_path(value: str | Path | None) -> Path:
    """Resolve a current or legacy media path strictly inside MEDIA_CACHE_DIR."""
    raw = str(value or "").strip()
    if not raw:
        raise MediaPathError("missing_media_path")
    path = Path(raw.replace("\\", "/"))
    if any(part in {"", ".", ".."} for part in path.parts):
        raise MediaPathError("media_path_traversal")

    cache_root = config.MEDIA_CACHE_DIR.resolve()
    if path.is_absolute() or PureWindowsPath(raw).is_absolute():
        try:
            candidate = path.resolve()
            candidate.relative_to(cache_root)
        except (OSError, ValueError):
            parts = list(path.parts)
            try:
                marker = next(
                    index for index, part in enumerate(parts)
                    if part.casefold() == "media_cache"
                )
            except StopIteration as exc:
                raise MediaPathError("absolute_media_path_outside_cache") from exc
            suffix = parts[marker + 1 :]
            if not suffix or any(part in {"", ".", ".."} for part in suffix):
                raise MediaPathError("invalid_legacy_media_suffix") from None
            candidate = cache_root.joinpath(*suffix).resolve()
    else:
        parts = list(path.parts)
        if parts and parts[0].casefold() == "media_cache":
            parts = parts[1:]
        if not parts:
            raise MediaPathError("missing_media_path_suffix")
        candidate = cache_root.joinpath(*parts).resolve()

    try:
        candidate.relative_to(cache_root)
    except ValueError as exc:
        raise MediaPathError("media_path_outside_cache") from exc
    if _contains_symlink(cache_root, candidate):
        raise MediaPathError("media_path_symlink_escape")
    return candidate


def portable_media_path(value: str | Path | None) -> str:
    """Return a checkout-portable path for media metadata.

    New sidecars must not embed the absolute path of the machine that captured
    them.  Paths outside the current checkout are re-anchored at the stable
    ``media_cache`` marker (which also handles old ``C:\\WikiRag`` paths).
    Runtime consumers can resolve the resulting relative path against the
    active checkout.
    """

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        resolved = resolve_media_path(raw)
    except MediaPathError:
        return ""
    relative = resolved.relative_to(config.MEDIA_CACHE_DIR.resolve())
    return Path("media_cache", *relative.parts).as_posix()


def portable_project_path(value: str | Path | None) -> str:
    """Return a relative path for artifacts stored under the checkout."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(config.PROJECT_ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        return ""
