import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import cold_provenance, release_identity, sync_media_contract
from scripts.build_release_verification import _cold_graph_is_strict


def test_sync_media_cli_stamps_manifest_bound_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"schema_version": "accepted_manifest_v2", "run_id": "release-run", "sources": []}),
        encoding="utf-8",
    )
    report_path = tmp_path / "media.json"
    identity = {
        "release_run_id": "release-run",
            "manifest_path": str(manifest_path.resolve()),
            "manifest_sha256": "a" * 64,
            "corpus_identity_sha256": "b" * 64,
    }
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sync_media_contract.py",
            "--report",
            str(report_path),
            "--manifest",
            str(manifest_path),
            "--release-run-id",
            "release-run",
        ],
    )
    with (
        patch.object(
            sync_media_contract,
            "load_manifest_file",
            return_value=SimpleNamespace(
                payload={"schema_version": "accepted_manifest_v2", "run_id": "release-run", "sources": []},
                errors=[],
                manifest_bytes=manifest_path.read_bytes(),
            ),
        ),
        patch.object(
            sync_media_contract,
            "sync_media_contract",
            return_value={
                "schema_version": "media_contract_sync_v1",
                "status": "passed",
                "apply": False,
                "candidates": 0,
                "changed": 0,
                "mismatches": 0,
                "errors": [],
            },
        ),
        patch.object(sync_media_contract, "build_identity", return_value=identity),
    ):
        assert sync_media_contract.main() == 0

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["release_schema_version"] == "release_evidence_v1"
    assert payload["release_identity"] == identity
    assert "--manifest" in payload["command"]
    assert "--release-run-id release-run" in payload["command"]


def test_sync_media_cli_requires_manifest_and_run_id_together(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync_media_contract.py", "--report", str(tmp_path / "media.json"), "--manifest", "manifest.json"],
    )
    with pytest.raises(SystemExit):
        sync_media_contract.main()


def test_sync_media_cli_rejects_run_mismatch_before_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"schema_version": "accepted_manifest_v2", "run_id": "manifest-run", "sources": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sync_media_contract.py",
            "--apply",
            "--report",
            str(tmp_path / "media.json"),
            "--manifest",
            str(manifest_path),
            "--release-run-id",
            "other-run",
        ],
    )
    with patch.object(sync_media_contract, "sync_media_contract") as scan:
        with patch.object(
            sync_media_contract,
            "load_manifest_file",
            return_value=SimpleNamespace(
                payload={"schema_version": "accepted_manifest_v2", "run_id": "manifest-run", "sources": []},
                errors=[],
                manifest_bytes=manifest_path.read_bytes(),
            ),
        ):
            with pytest.raises(ValueError, match="manifest_run_id_mismatch"):
                sync_media_contract.main()
    scan.assert_not_called()


def test_cold_graph_verifier_requires_terminal_per_source_hashes_and_duration(tmp_path: Path) -> None:
    normalized_path = tmp_path / "source.txt"
    normalized_path.write_text("source", encoding="utf-8")
    card_path = tmp_path / "source.enriched.json"
    graph_text = "Entity: source\nRelation: test"
    card_path.write_text(
        json.dumps(
            {
                "schema_version": "enriched_v2",
                "graph_text": graph_text,
                "provenance": {
                    "source_id": "source:one",
                    "normalized_path": str(normalized_path.resolve()),
                },
            }
        ),
        encoding="utf-8",
    )
    sources = [
        {
            "source_id": "source:one",
            "normalized_path": str(normalized_path.resolve()),
            "card_path": str(card_path.resolve()),
            "normalized_sha256": cold_provenance.sha256_file(normalized_path),
            "enriched_sha256": cold_provenance.sha256_file(card_path),
            "graph_text_sha256": release_identity.sha256_bytes(graph_text.encode("utf-8")),
        }
    ]
    source_proof = cold_provenance.compute_source_proof({"sources": sources})
    target = tmp_path / "cold" / "rag_storage"
    target.mkdir(parents=True)
    doc_id = source_proof["doc_ids"][0]
    (target / "kv_store_doc_status.json").write_text(
        json.dumps({doc_id: {"status": "processed"}}), encoding="utf-8"
    )
    (target / "kv_store_full_docs.json").write_text(
        json.dumps({doc_id: {"content": graph_text}}), encoding="utf-8"
    )
    loaded_proof = cold_provenance.loaded_doc_proof(source_proof, target)
    inventory = cold_provenance.storage_inventory(target)
    measured = source_proof["records"][0]
    loaded = loaded_proof["records"][0]
    workspace = target.parent
    runtime = release_identity.runtime_identity()
    payload = {
        "source_storage_copied": False,
        "empty_before": True,
        "workspace": str(workspace),
        "candidate_path": str(target),
        "source_ids": ["source:one"],
        "doc_ids": [doc_id],
        "source_proof_before_load": source_proof,
        "source_proof_sha256": source_proof["source_proof_sha256"],
        "loaded_doc_proof": loaded_proof,
        "storage_inventory": inventory,
        "release_identity": {
            "runtime_identity": runtime,
        },
        "effective_runtime_identity": runtime,
        "workspace_inventory": {
            "checked": True,
            "source_storage_copied": False,
        },
        "effective_cold_runtime": release_identity.effective_cold_runtime(
            release_identity.COLD_GRAPH_BATCH_SIZE_DEFAULT,
            release_identity.COLD_GRAPH_BATCH_TIMEOUT_SECONDS_DEFAULT,
        ),
        "batch_size": release_identity.COLD_GRAPH_BATCH_SIZE_DEFAULT,
        "batch_timeout_seconds": release_identity.COLD_GRAPH_BATCH_TIMEOUT_SECONDS_DEFAULT,
        "config_identity_sha256": release_identity.config_identity_sha256(),
        "cold_graph_attempt": {
            "status": "passed",
            "expected": 1,
            "processed": 1,
            "processing": 0,
            "failed": 0,
            "unknown": 0,
            "loader_returned": 1,
            "inserted_by_loader": 1,
            "reused_processed": 0,
            "processed_terminal": 1,
            "source_ids": ["source:one"],
            "doc_ids": [doc_id],
            "lineage": {
                "inserted_source_ids": ["source:one"],
                "reused_processed_source_ids": [],
                "processed_terminal_source_ids": ["source:one"],
            },
            "sources": [
                {
                    "source_id": "source:one",
                    "status": "processed",
                    "terminal_status": "processed",
                    "normalized_sha256": measured["normalized_sha256"],
                    "enriched_sha256": measured["enriched_sha256"],
                    "graph_text_sha256": measured["graph_text_sha256"],
                    "measured_normalized_sha256": measured["normalized_sha256"],
                    "measured_enriched_sha256": measured["enriched_sha256"],
                    "measured_graph_text_sha256": measured["graph_text_sha256"],
                    "graph_text_for_load_sha256": measured["graph_text_for_load_sha256"],
                    "loaded_full_doc_sha256": loaded["loaded_full_doc_sha256"],
                    "loaded_full_doc_matches": True,
                    "duration_ms": 1250.0,
                    "error": None,
                }
            ],
        },
    }
    assert _cold_graph_is_strict(payload, sources)

    payload["cold_graph_attempt"]["sources"][0].pop("enriched_sha256")
    assert not _cold_graph_is_strict(payload, sources)
    payload["cold_graph_attempt"]["sources"][0]["enriched_sha256"] = measured["enriched_sha256"]
    payload["cold_graph_attempt"]["sources"][0].pop("duration_ms")
    assert not _cold_graph_is_strict(payload, sources)
    payload["cold_graph_attempt"]["sources"][0]["duration_ms"] = 1250.0
    payload["batch_size"] += 1
    assert not _cold_graph_is_strict(payload, sources)
    payload["batch_size"] -= 1
    payload["source_storage_copied"] = True
    assert not _cold_graph_is_strict(payload, sources)
    payload["source_storage_copied"] = False
    original_source = normalized_path.read_bytes()
    normalized_path.write_bytes(original_source + b" tampered")
    assert not _cold_graph_is_strict(payload, sources)
    normalized_path.write_bytes(original_source)
    payload["effective_cold_runtime"]["batch_timeout_seconds"] += 1
    assert not _cold_graph_is_strict(payload, sources)
    payload["effective_cold_runtime"]["batch_timeout_seconds"] -= 1
    payload.pop("effective_cold_runtime")
    assert not _cold_graph_is_strict(payload, sources)
