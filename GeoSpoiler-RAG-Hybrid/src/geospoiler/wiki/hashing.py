"""Deterministic canonicalization and hashing for Wiki v2 state."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from typing import Any, TypeAlias

JsonPath: TypeAlias = tuple[str, ...]
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def normalize_text(value: str) -> str:
    """Normalize ordinary text without changing internal whitespace or meaning."""
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def normalize_exact_quote(value: str) -> str:
    """Conservatively normalize a quote while preserving its exact internal text."""
    return normalize_text(value)


def canonicalize(
    value: Any,
    *,
    unordered_collection_paths: Collection[JsonPath] = (),
    exact_quote_paths: Collection[JsonPath] = (),
) -> JsonValue:
    """Return a JSON-compatible value with deterministic keys and selected arrays.

    Collections remain ordered unless their exact JSON path is explicitly listed
    in ``unordered_collection_paths``. Array elements use ``"*"`` in descendant
    paths, so ``("quotes", "*", "text")`` identifies every quote text field.
    """
    unordered_paths = frozenset(unordered_collection_paths)
    quote_paths = frozenset(exact_quote_paths)
    return _canonicalize(
        value,
        path=(),
        unordered_collection_paths=unordered_paths,
        exact_quote_paths=quote_paths,
    )


def canonical_json(
    value: Any,
    *,
    unordered_collection_paths: Collection[JsonPath] = (),
    exact_quote_paths: Collection[JsonPath] = (),
) -> str:
    """Serialize a value as stable UTF-8 JSON."""
    canonical = canonicalize(
        value,
        unordered_collection_paths=unordered_collection_paths,
        exact_quote_paths=exact_quote_paths,
    )
    return _dump_canonical(canonical)


def sha256_hex(value: str | bytes) -> str:
    """Return the lowercase SHA-256 hex digest for text or bytes."""
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def content_hash(
    value: Any,
    *,
    namespace: str | None = None,
    unordered_collection_paths: Collection[JsonPath] = (),
    exact_quote_paths: Collection[JsonPath] = (),
) -> str:
    """Hash canonical JSON, optionally separating the hash by namespace."""
    payload = canonical_json(
        value,
        unordered_collection_paths=unordered_collection_paths,
        exact_quote_paths=exact_quote_paths,
    )
    if namespace is not None:
        payload = f"{normalize_text(namespace)}\n{payload}"
    return f"sha256:{sha256_hex(payload)}"


def content_fingerprint(
    value: Any,
    *,
    unordered_collection_paths: Collection[JsonPath] = (),
    exact_quote_paths: Collection[JsonPath] = (),
) -> str:
    """Return a deterministic content fingerprint suitable for sorting."""
    return content_hash(
        value,
        namespace="wiki-v2-content-fingerprint",
        unordered_collection_paths=unordered_collection_paths,
        exact_quote_paths=exact_quote_paths,
    )


def _canonicalize(
    value: Any,
    *,
    path: JsonPath,
    unordered_collection_paths: frozenset[JsonPath],
    exact_quote_paths: frozenset[JsonPath],
) -> JsonValue:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("Non-finite floats are not valid canonical JSON")
        return value
    if isinstance(value, str):
        if _path_matches(path, exact_quote_paths):
            return normalize_exact_quote(value)
        return normalize_text(value)
    if isinstance(value, Mapping):
        canonical_mapping: dict[str, JsonValue] = {}
        for raw_key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            key = normalize_text(str(raw_key))
            if key in canonical_mapping:
                raise ValueError(f"Canonical JSON key collision at {path!r}: {key!r}")
            canonical_mapping[key] = _canonicalize(
                item,
                path=(*path, key),
                unordered_collection_paths=unordered_collection_paths,
                exact_quote_paths=exact_quote_paths,
            )
        return canonical_mapping
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        canonical_items = [
            _canonicalize(
                item,
                path=(*path, "*"),
                unordered_collection_paths=unordered_collection_paths,
                exact_quote_paths=exact_quote_paths,
            )
            for item in value
        ]
        if path in unordered_collection_paths:
            canonical_items.sort(key=lambda item: (content_fingerprint(item), _dump_canonical(item)))
        return canonical_items
    raise TypeError(f"Unsupported canonical JSON value at {path!r}: {type(value).__name__}")


def _path_matches(path: JsonPath, candidates: frozenset[JsonPath]) -> bool:
    return any(
        len(candidate) == len(path)
        and all(expected == "*" or expected == actual for expected, actual in zip(candidate, path, strict=True))
        for candidate in candidates
    )


def _dump_canonical(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
