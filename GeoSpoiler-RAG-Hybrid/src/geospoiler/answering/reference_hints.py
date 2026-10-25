"""Reference merging and source path normalization helpers."""

from typing import Any

from geospoiler import config


def _existing_references(result: dict[str, Any]) -> list[dict[str, Any]]:
    data = result.get("data") if isinstance(result, dict) else {}
    references = data.get("references", []) if isinstance(data, dict) else []
    return [ref for ref in references if isinstance(ref, dict)]


def _merge_references(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = []
    seen = set()
    for group in groups:
        for ref in group:
            file_path = str(ref.get("file_path") or "").strip()
            ref_id = str(ref.get("reference_id") or "").strip()
            key = file_path or ref_id
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(dict(ref))
    return merged


def _resolve_match_source_path(source_path: str) -> str:
    path = config.resolve_project_path(source_path)
    return str(path.resolve(strict=False))


def _attach_reference_hints(result: dict[str, Any], question: str) -> dict[str, Any]:
    return result
