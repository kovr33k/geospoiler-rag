from __future__ import annotations

import pytest

from retrieval.wiki.hashing import (
    canonical_json,
    content_hash,
    normalize_exact_quote,
    normalize_text,
)


def test_unicode_and_line_endings_are_canonical() -> None:
    assert normalize_text("  Cafe\u0301\r\nline\r  ") == "Café\nline"
    assert content_hash({"text": "Cafe\u0301\r\nline"}) == content_hash(
        {"text": "Café\nline"}
    )


def test_exact_quote_normalization_preserves_internal_whitespace() -> None:
    assert normalize_exact_quote("  exact  words\r\nnext  ") == "exact  words\nnext"
    payload = {"quotes": [{"text": "  exact  words\r\nnext  "}]}
    assert canonical_json(
        payload,
        exact_quote_paths=[("quotes", "*", "text")],
    ) == '{"quotes":[{"text":"exact  words\\nnext"}]}'


def test_only_explicitly_unordered_collections_are_sorted() -> None:
    first = {"entities": [{"name": "B"}, {"name": "A"}]}
    second = {"entities": [{"name": "A"}, {"name": "B"}]}

    assert content_hash(first) != content_hash(second)
    assert content_hash(
        first,
        unordered_collection_paths=[("entities",)],
    ) == content_hash(
        second,
        unordered_collection_paths=[("entities",)],
    )


def test_canonical_json_sorts_object_keys_but_rejects_non_finite_numbers() -> None:
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    with pytest.raises(ValueError, match="Non-finite"):
        canonical_json({"value": float("nan")})
