import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import config
import llm_backend
from scripts import cold_graph_check, release_identity
from scripts.build_release_verification import _cold_graph_is_strict


def _manifest(tmp_path: Path, count: int = 2) -> Path:
    sources = []
    for index in range(count):
        source_path = (tmp_path / f"source-{index}.txt").resolve()
        source_text = f"source {index}"
        source_path.write_text(source_text, encoding="utf-8")
        card_path = (tmp_path / f"source-{index}.enriched.json").resolve()
        graph_text = f"Entity: source {index}\nRelation: test"
        card_path.write_text(
            json.dumps(
                {
                    "schema_version": "enriched_v2",
                    "graph_text": graph_text,
                    "provenance": {
                        "source_id": f"source:{index}",
                        "normalized_path": str(source_path),
                    },
                }
            ),
            encoding="utf-8",
        )
        sources.append(
            {
                "accepted": True,
                "source_id": f"source:{index}",
                "normalized_path": str(source_path),
                "card_path": str(card_path),
                "normalized_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "enriched_sha256": hashlib.sha256(card_path.read_bytes()).hexdigest(),
                "graph_text_sha256": hashlib.sha256(graph_text.encode("utf-8")).hexdigest(),
            }
        )
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "accepted_manifest_v2",
                "run_id": "run-1",
                "pipeline_identity": {
                    "llm_profile": config.LLM_PROFILE,
                    "enrichment_model": llm_backend.active_model_for("enrichment"),
                    "media_contract_version": "media-contract-v2",
                    "late_fusion_enabled": config.LATE_FUSION_ENABLED,
                    "reranker_enabled": config.RERANKER_ENABLED,
                    "wiki_enabled": config.WIKI_ENABLED,
                    "expected_cards": count,
                    "config_identity_sha256": release_identity.config_identity_sha256(),
                },
                "sources": sources,
            }
        ),
        encoding="utf-8",
    )
    return path


def _status_for_batch(batch_manifest: Path, target: Path) -> int:
    payload = json.loads(batch_manifest.read_text(encoding="utf-8"))
    status_path = target / "kv_store_doc_status.json"
    full_docs_path = target / "kv_store_full_docs.json"
    current = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    full_docs = json.loads(full_docs_path.read_text(encoding="utf-8")) if full_docs_path.exists() else {}
    for entry in payload["sources"]:
        doc_id = cold_graph_check._source_doc_id(entry["normalized_path"])
        card = json.loads(Path(entry["card_path"]).read_text(encoding="utf-8"))
        current[doc_id] = {
            "status": "processed",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:01+00:00",
            "metadata": {
                "processing_start_time": 100,
                "processing_end_time": 101,
            },
        }
        full_docs[doc_id] = {"content": card["graph_text"]}
    target.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(current), encoding="utf-8")
    full_docs_path.write_text(json.dumps(full_docs), encoding="utf-8")
    return len(payload["sources"])


def _run_check(
    workspace: Path,
    manifest: Path,
    fake_load,
    *,
    resume: bool = False,
    batch_size: int = 1,
    batch_timeout_seconds: float = cold_graph_check.DEFAULT_BATCH_TIMEOUT_SECONDS,
):
    async def finalize(_rag):
        return None

    with (
        patch.object(cold_graph_check, "create_rag", new=AsyncMock(return_value=object())),
        patch.object(cold_graph_check, "load_from_manifest", side_effect=fake_load),
        patch.object(cold_graph_check, "_finalize_rag_safely", side_effect=finalize),
        patch.object(cold_graph_check.config, "NORMALIZED_DIR", workspace.parent),
        patch.object(cold_graph_check.config, "YOUTUBE_NORMALIZED_DIR", workspace.parent),
        patch.object(cold_graph_check.config, "ENRICHED_DIR", workspace.parent),
    ):
        return asyncio.run(
            cold_graph_check._check(
                workspace,
                manifest,
                resume=resume,
                batch_size=batch_size,
                batch_timeout_seconds=batch_timeout_seconds,
            )
        )


def test_cold_graph_isolates_loader_reports_from_project_artifacts(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, count=1)
    workspace = tmp_path / "cold"
    project_report = cold_graph_check.PROJECT_ROOT / "artifacts" / "accepted_manifest_load_report.json"
    before = project_report.read_bytes() if project_report.exists() else None

    async def fake_load(_rag, batch_manifest: Path):
        accepted_report = cold_graph_check.ingest_module._accepted_manifest_report_path()
        skipped_report = cold_graph_check.ingest_module._skipped_insert_report_path()
        assert accepted_report == workspace / "accepted_manifest_load_report.json"
        assert skipped_report == workspace / "rag_insert_skipped.md"
        accepted_report.write_text("{}", encoding="utf-8")
        return _status_for_batch(batch_manifest, cold_graph_check.config.RAG_STORAGE_DIR)

    report = _run_check(workspace, manifest, fake_load)
    assert report["status"] == "passed"
    assert Path(report["accepted_manifest_load_report_path"]) == workspace / "accepted_manifest_load_report.json"
    assert (workspace / "accepted_manifest_load_report.json").exists()
    assert (project_report.read_bytes() if project_report.exists() else None) == before


def test_successful_cold_report_round_trips_through_release_contract(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, count=1)
    workspace = tmp_path / "cold"

    async def fake_load(_rag, batch_manifest: Path):
        return _status_for_batch(batch_manifest, cold_graph_check.config.RAG_STORAGE_DIR)

    report = _run_check(workspace, manifest, fake_load)
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_bytes = manifest.read_bytes()
    identity = release_identity.build_identity(
        "run-1",
        manifest,
        manifest_payload,
        manifest_bytes=manifest_bytes,
    )
    stamped = release_identity.stamp_report(
        report,
        identity,
        "python scripts/cold_graph_check.py",
        producer="scripts.cold_graph_check",
        producer_schema_version="cold_graph_extraction_v1",
        source_inputs={
            "manifest_sha256": identity["manifest_sha256"],
            "corpus_identity_sha256": identity["corpus_identity_sha256"],
        },
        command_argv=["python", "scripts/cold_graph_check.py"],
    )

    assert release_identity.validate_report_envelope(
        stamped,
        kind="cold_graph",
        identity=identity,
    ) == []
    assert _cold_graph_is_strict(stamped, manifest_payload["sources"])


def test_manifest_byte_change_during_cold_run_is_failed_closed(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, count=1)
    workspace = tmp_path / "cold"
    original = manifest.read_bytes()

    async def fake_load(_rag, batch_manifest: Path):
        result = _status_for_batch(batch_manifest, cold_graph_check.config.RAG_STORAGE_DIR)
        manifest.write_bytes(original + b"\n")
        return result

    with pytest.raises(RuntimeError, match="manifest_changed_during_cold_run"):
        _run_check(workspace, manifest, fake_load)


def test_cold_graph_actual_skipped_writer_is_isolated(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, count=1)
    workspace = tmp_path / "cold"
    project_report = cold_graph_check.PROJECT_ROOT / "artifacts" / "rag_insert_skipped.md"
    before = project_report.read_bytes() if project_report.exists() else None

    async def fake_load(_rag, batch_manifest: Path):
        target = cold_graph_check.config.RAG_STORAGE_DIR
        inserted = _status_for_batch(batch_manifest, target)
        cold_graph_check.storage_module._write_skipped_insert_report(
            [{"source_path": "cold-test", "reason": "test", "content_length": 1}]
        )
        return inserted

    report = _run_check(workspace, manifest, fake_load)
    assert report["status"] == "passed"
    isolated_report = workspace / "rag_insert_skipped.md"
    assert isolated_report.exists()
    assert "cold-test" in isolated_report.read_text(encoding="utf-8")
    assert (project_report.read_bytes() if project_report.exists() else None) == before


def test_cold_graph_resume_uses_checkpoint_after_interruption(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, count=2)
    workspace = tmp_path / "cold"
    calls = 0

    async def interrupting_load(_rag, batch_manifest: Path):
        nonlocal calls
        calls += 1
        inserted = _status_for_batch(batch_manifest, cold_graph_check.config.RAG_STORAGE_DIR)
        if calls == 1:
            raise KeyboardInterrupt
        return inserted

    with pytest.raises(KeyboardInterrupt):
        _run_check(workspace, manifest, interrupting_load)

    checkpoint = workspace / "cold_graph_checkpoint.json"
    assert checkpoint.exists()
    checkpoint_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert checkpoint_payload["manifest_sha256"]
    assert checkpoint_payload["run_id"] == "run-1"
    assert checkpoint_payload["config_identity_sha256"]
    assert checkpoint_payload["completed_source_ids"] == []

    async def completing_load(_rag, batch_manifest: Path, **_kwargs):
        return _status_for_batch(batch_manifest, cold_graph_check.config.RAG_STORAGE_DIR)

    report = _run_check(workspace, manifest, completing_load, resume=True)
    assert report["status"] == "passed"
    assert report["cold_graph_attempt"]["expected"] == 2
    assert report["cold_graph_attempt"]["loader_returned"] == 1
    assert report["cold_graph_attempt"]["processed"] == 2
    assert report["cold_graph_attempt"]["inserted_by_loader"] == 1
    assert report["cold_graph_attempt"]["reused_processed"] == 1
    assert report["cold_graph_attempt"]["processed_terminal"] == 2
    assert all(item["terminal_status"] == "processed" for item in report["source_results"])
    assert all(item["duration_ms"] == 1000 for item in report["source_results"])
    assert report["effective_cold_runtime"]["override"] is False
    assert report["source_ids"] == ["source:0", "source:1"]
    assert len(report["doc_ids"]) == 2
    assert report["storage_inventory"]["checked"] is True
    assert all(item["loaded_full_doc_matches"] is True for item in report["source_results"])
    assert (
        json.loads(checkpoint.read_text(encoding="utf-8"))["effective_cold_runtime"] == report["effective_cold_runtime"]
    )
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["completed_source_ids"] == [
        "source:0",
        "source:1",
    ]


def test_cold_graph_resume_rejects_manifest_or_batch_identity_mismatch(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, count=1)
    workspace = tmp_path / "cold"

    async def fake_load(_rag, batch_manifest: Path):
        return _status_for_batch(batch_manifest, cold_graph_check.config.RAG_STORAGE_DIR)

    _run_check(workspace, manifest, fake_load)
    checkpoint = workspace / "cold_graph_checkpoint.json"
    original_manifest = manifest.read_text(encoding="utf-8")
    manifest.write_text(original_manifest + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest_sha256 mismatch"):
        _run_check(workspace, manifest, fake_load, resume=True)
    manifest.write_text(original_manifest, encoding="utf-8")

    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["batch_size"] = 2
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="batch_size mismatch"):
        _run_check(workspace, manifest, fake_load, resume=True)


def test_cold_graph_batch_size_is_bounded(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, count=1)
    with pytest.raises(ValueError, match="between 1 and"):
        asyncio.run(cold_graph_check._check(tmp_path / "cold", manifest, batch_size=26))


def test_cold_graph_inventory_rejects_storage_copy(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, count=1)
    workspace = tmp_path / "cold"
    copied = workspace / "copied" / "rag_storage"
    copied.mkdir(parents=True)
    (copied / "kv_store_doc_status.json").write_text("{}", encoding="utf-8")
    with (
        patch.object(cold_graph_check.config, "NORMALIZED_DIR", tmp_path),
        patch.object(cold_graph_check.config, "YOUTUBE_NORMALIZED_DIR", tmp_path),
        patch.object(cold_graph_check.config, "ENRICHED_DIR", tmp_path),
        pytest.raises(RuntimeError, match="workspace inventory"),
    ):
        asyncio.run(cold_graph_check._check(workspace, manifest))


def test_runtime_identity_contains_rag_operation_timeouts() -> None:
    identity = release_identity.runtime_identity()
    assert identity["rag_runtime"] == {
        "insert_timeout_seconds": release_identity.config.RAG_INSERT_TIMEOUT_SECONDS,
        "delete_timeout_seconds": release_identity.config.RAG_DELETE_TIMEOUT_SECONDS,
        "finalize_timeout_seconds": release_identity.config.RAG_FINALIZE_TIMEOUT_SECONDS,
    }
    baseline = release_identity.config_identity_sha256()
    with patch.object(
        release_identity.config,
        "RAG_INSERT_TIMEOUT_SECONDS",
        release_identity.config.RAG_INSERT_TIMEOUT_SECONDS + 1,
    ):
        assert release_identity.config_identity_sha256() != baseline


def test_cold_graph_batch_timeout_is_enforced_and_checkpointed(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, count=1)
    workspace = tmp_path / "cold"

    async def hanging_load(_rag, _batch_manifest: Path):
        await asyncio.sleep(0.05)
        return 1

    with pytest.raises(asyncio.TimeoutError):
        _run_check(workspace, manifest, hanging_load, batch_size=1, batch_timeout_seconds=0.001)

    checkpoint = json.loads((workspace / "cold_graph_checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["batch_timeout_seconds"] == 0.001
    assert checkpoint["completed_source_ids"] == []


def test_cold_graph_batch_timeout_must_be_positive_and_finite(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, count=1)
    with pytest.raises(ValueError, match="finite and greater than zero"):
        asyncio.run(cold_graph_check._check(tmp_path / "cold", manifest, batch_timeout_seconds=0))
    with pytest.raises(ValueError, match="finite and greater than zero"):
        asyncio.run(cold_graph_check._check(tmp_path / "cold2", manifest, batch_timeout_seconds=float("inf")))
