import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import release_identity


def test_runtime_identity_contains_reasoning_and_concurrency_contract() -> None:
    identity = release_identity.runtime_identity()

    llm_runtime = identity["llm_runtime"]
    assert llm_runtime["reasoning_effort"] == release_identity.config.LLM_REASONING_EFFORT
    assert llm_runtime["codex_luna_reasoning_effort"] == release_identity.config.CODEX_LUNA_REASONING_EFFORT
    assert llm_runtime["codex_max_concurrency"] == release_identity.config.CODEX_LLM_MAX_CONCURRENCY
    assert identity["enrichment_runtime"]["concurrency"] == release_identity.config.ENRICHMENT_CONCURRENCY
    assert identity["embedding"]["concurrency"] == release_identity.config.EMBEDDING_CONCURRENCY
    assert "api_key" not in json.dumps(identity, ensure_ascii=False).casefold()


def test_runtime_identity_hash_changes_when_reasoning_or_concurrency_changes() -> None:
    baseline = release_identity.config_identity_sha256()
    with patch.object(
        release_identity.config,
        "CODEX_LUNA_REASONING_EFFORT",
        f"{release_identity.config.CODEX_LUNA_REASONING_EFFORT}-test",
    ):
        assert release_identity.config_identity_sha256() != baseline

    with patch.object(
        release_identity.config,
        "CODEX_LLM_MAX_CONCURRENCY",
        release_identity.config.CODEX_LLM_MAX_CONCURRENCY + 1,
    ):
        assert release_identity.config_identity_sha256() != baseline


def test_build_identity_rejects_manifest_run_id_mismatch(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest = {"schema_version": "accepted_manifest_v2", "run_id": "manifest-run", "sources": []}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest_run_id_mismatch"):
        release_identity.build_identity("other-run", manifest_path, manifest)


def test_build_identity_rejects_missing_manifest_run_id(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest = {"schema_version": "accepted_manifest_v2", "sources": []}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest run_id must be non-empty"):
        release_identity.build_identity("release-run", manifest_path, manifest)
