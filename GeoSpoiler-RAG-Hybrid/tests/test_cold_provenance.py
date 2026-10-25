import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import llm_backend
from scripts import cold_provenance
from scripts.release_identity import config_identity_sha256


def _fixture(tmp_path: Path) -> tuple[dict, dict, Path, str]:
    source = tmp_path / "source.txt"
    source.write_text("source body", encoding="utf-8")
    card = tmp_path / "source.enriched.json"
    graph_text = "Entity: source\nRelation: test"
    card.write_text(
        json.dumps(
            {
                "schema_version": "enriched_v2",
                "graph_text": graph_text,
                "provenance": {
                    "source_id": "source:one",
                    "normalized_path": str(source.resolve()),
                },
            }
        ),
        encoding="utf-8",
    )
    source_entry = {
        "accepted": True,
        "source_id": "source:one",
        "normalized_path": str(source.resolve()),
        "card_path": str(card.resolve()),
        "normalized_sha256": cold_provenance.sha256_file(source),
        "enriched_sha256": cold_provenance.sha256_file(card),
        "graph_text_sha256": cold_provenance._sha256_bytes(graph_text.encode("utf-8")),
    }
    manifest = {"sources": [source_entry]}
    source_proof = cold_provenance.compute_source_proof(manifest)
    target = tmp_path / "cold" / "rag_storage"
    target.mkdir(parents=True)
    doc_id = source_proof["doc_ids"][0]
    (target / "kv_store_doc_status.json").write_text(
        json.dumps({doc_id: {"status": "processed"}}), encoding="utf-8"
    )
    (target / "kv_store_full_docs.json").write_text(
        json.dumps({doc_id: {"content": graph_text}}), encoding="utf-8"
    )
    return manifest, source_proof, target, graph_text


def test_source_proof_recomputes_normalized_and_card_bytes(tmp_path: Path) -> None:
    manifest, _proof, _target, _graph_text = _fixture(tmp_path)
    source_path = Path(manifest["sources"][0]["normalized_path"])
    source_path.write_text("tampered source", encoding="utf-8")
    with pytest.raises(ValueError, match="normalized_sha256 mismatch"):
        cold_provenance.compute_source_proof(manifest)


def test_v2_source_proof_accepts_uppercase_manifest_hashes(tmp_path: Path) -> None:
    manifest, _proof, _target, _graph_text = _fixture(tmp_path)
    entry = manifest["sources"][0]
    for field in ("normalized_sha256", "enriched_sha256", "graph_text_sha256"):
        entry[field] = entry[field].upper()
    manifest.update(
        {
            "schema_version": "accepted_manifest_v2",
            "run_id": "run-1",
            "pipeline_identity": {
                "llm_profile": cold_provenance.config.LLM_PROFILE,
                "enrichment_model": llm_backend.active_model_for("enrichment"),
                "media_contract_version": "media-contract-v2",
                "late_fusion_enabled": cold_provenance.config.LATE_FUSION_ENABLED,
                "reranker_enabled": cold_provenance.config.RERANKER_ENABLED,
                "wiki_enabled": cold_provenance.config.WIKI_ENABLED,
                "expected_cards": 1,
                "config_identity_sha256": config_identity_sha256(),
            },
        }
    )

    with (
        patch.object(cold_provenance.config, "NORMALIZED_DIR", tmp_path),
        patch.object(cold_provenance.config, "YOUTUBE_NORMALIZED_DIR", tmp_path),
        patch.object(cold_provenance.config, "ENRICHED_DIR", tmp_path),
    ):
        proof = cold_provenance.compute_source_proof(manifest)

    assert proof["records"][0]["normalized_sha256"] == entry["normalized_sha256"].lower()


def test_loaded_doc_proof_requires_exact_doc_set_and_content(tmp_path: Path) -> None:
    _manifest, proof, target, _graph_text = _fixture(tmp_path)
    status_path = target / "kv_store_doc_status.json"
    full_docs_path = target / "kv_store_full_docs.json"
    doc_id = proof["doc_ids"][0]
    status = json.loads(status_path.read_text(encoding="utf-8"))
    full_docs = json.loads(full_docs_path.read_text(encoding="utf-8"))
    status["unexpected-doc"] = {"status": "processed"}
    full_docs["unexpected-doc"] = {"content": "extra"}
    status_path.write_text(json.dumps(status), encoding="utf-8")
    full_docs_path.write_text(json.dumps(full_docs), encoding="utf-8")
    with pytest.raises(ValueError, match="doc_id set mismatch"):
        cold_provenance.loaded_doc_proof(proof, target)

    status.pop("unexpected-doc")
    full_docs.pop("unexpected-doc")
    full_docs[doc_id] = {"content": "changed"}
    status_path.write_text(json.dumps(status), encoding="utf-8")
    full_docs_path.write_text(json.dumps(full_docs), encoding="utf-8")
    with pytest.raises(ValueError, match="content hash mismatch"):
        cold_provenance.loaded_doc_proof(proof, target)


def test_storage_inventory_is_deterministic_and_changes_on_file_mutation(tmp_path: Path) -> None:
    _manifest, _proof, target, _graph_text = _fixture(tmp_path)
    first = cold_provenance.storage_inventory(target)
    second = cold_provenance.storage_inventory(target)
    assert first == second
    (target / "extra.json").write_text("extra", encoding="utf-8")
    changed = cold_provenance.storage_inventory(target)
    assert changed["checked"] is True
    assert changed["inventory_sha256"] != first["inventory_sha256"]


def test_storage_inventory_rejects_symlink_target(tmp_path: Path) -> None:
    real_target = tmp_path / "real"
    real_target.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(real_target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    inventory = cold_provenance.storage_inventory(link)
    assert inventory["checked"] is False
    assert any("symlink" in error for error in inventory["errors"])
