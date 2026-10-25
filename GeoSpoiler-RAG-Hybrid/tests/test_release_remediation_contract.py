import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from loader.ingest import _sha256_file
from scripts import cold_graph_check, release_identity, security_release_scan
from scripts.build_release_evidence import build_bundle
from scripts.build_release_verification import (
    _manifest_sources,
    _positive_int,
    _smoke_is_strict,
)
from scripts.smoke_contract import OFFICIAL_CASES_SHA256, official_cases_payload
from scripts.sync_media_contract import sync_media_contract


def test_working_tree_identity_hashes_untracked_content(tmp_path: Path) -> None:
    tracked = tmp_path / "new_release.py"
    tracked.write_text("one", encoding="utf-8")

    def fake_run(command, **_kwargs):
        if command[1:3] == ["diff", "--binary"]:
            return SimpleNamespace(stdout=b"tracked-diff")
        return SimpleNamespace(stdout=b"new_release.py\0")

    with (
        patch.object(release_identity.config, "PROJECT_ROOT", tmp_path),
        patch.object(release_identity.subprocess, "run", side_effect=fake_run),
    ):
        first = release_identity.working_tree_sha256()
        tracked.write_text("two", encoding="utf-8")
        second = release_identity.working_tree_sha256()
    assert first != second


def test_runtime_identity_contains_result_affecting_retrieval_contract() -> None:
    identity = release_identity.runtime_identity()
    assert identity["embedding"]["model"]
    assert identity["late_fusion"]["card_top_k"] > 0
    assert "candidate_pool" in identity["reranker"]
    assert "api_key" not in json.dumps(identity).casefold()


def test_evidence_builder_rejects_post_hoc_manifest_rebinding(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    manifest = staging / "manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": "accepted_manifest_v2", "run_id": "source", "sources": []}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="post-hoc manifest rebinding|non-empty path|staging root"):
        with patch("scripts.build_release_evidence.assert_clean_release_tree"):
            build_bundle(tmp_path / "bundle", manifest, "different", [], staging_root=staging)


def test_cold_graph_loads_into_empty_storage_without_copy(tmp_path: Path) -> None:
    source_path = (tmp_path / "one.txt").resolve()
    source_path.write_text("source one", encoding="utf-8")
    card_path = (tmp_path / "one.enriched.json").resolve()
    graph_text = "Entity: one\nRelation: test"
    card_path.write_text(
        json.dumps(
            {
                "schema_version": "enriched_v2",
                "graph_text": graph_text,
                "provenance": {"source_id": "one", "normalized_path": str(source_path)},
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source_id": "one",
                        "normalized_path": str(source_path),
                        "card_path": str(card_path),
                        "normalized_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                        "enriched_sha256": hashlib.sha256(card_path.read_bytes()).hexdigest(),
                        "graph_text_sha256": hashlib.sha256(graph_text.encode("utf-8")).hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    async def fake_load(_rag, batch_manifest):
        target = cold_graph_check.config.RAG_STORAGE_DIR
        target.mkdir(parents=True, exist_ok=True)
        batch = json.loads(Path(batch_manifest).read_text(encoding="utf-8"))
        entry = batch["sources"][0]
        doc_id = cold_graph_check._source_doc_id(entry["normalized_path"])
        (target / "kv_store_doc_status.json").write_text(
            json.dumps({doc_id: {"status": "processed"}}), encoding="utf-8"
        )
        (target / "kv_store_full_docs.json").write_text(
            json.dumps({doc_id: {"content": graph_text}}), encoding="utf-8"
        )
        return 1

    async def finalize(_rag):
        return None

    with (
        patch.object(cold_graph_check, "create_rag", new=AsyncMock(return_value=object())),
        patch.object(cold_graph_check, "load_from_manifest", side_effect=fake_load),
        patch.object(cold_graph_check, "_finalize_rag_safely", side_effect=finalize),
    ):
        report = asyncio.run(cold_graph_check._check(tmp_path / "workspace", manifest))
    assert report["status"] == "passed"
    assert report["source_storage_copied"] is False
    assert report["empty_before"] is True


def test_cold_graph_records_source_terminal_status_duration_and_error(tmp_path: Path) -> None:
    source_path = (tmp_path / "one.txt").resolve()
    manifest = {"sources": [{"source_id": "one", "normalized_path": str(source_path)}]}
    doc_id = cold_graph_check._source_doc_id(str(source_path))
    results = cold_graph_check._source_results(
        manifest,
        {
            doc_id: {
                "status": "processed",
                "metadata": {"processing_start_time": 10, "processing_end_time": 12.5},
            }
        },
    )
    assert results == [
        {
            "index": 0,
            "source_id": "one",
            "normalized_path": str(source_path),
            "doc_id": doc_id,
            "status": "processed",
            "terminal_status": "processed",
            "duration_seconds": 2.5,
            "error": None,
        }
    ]


def test_cold_graph_main_writes_atomic_failed_report_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"sources": [{"source_id": "one"}]}), encoding="utf-8")
    report_path = tmp_path / "cold.json"

    async def stop_run(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cold_graph_check, "_check", stop_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cold_graph_check.py",
            "--workspace",
            str(tmp_path / "workspace"),
            "--manifest",
            str(manifest),
            "--report",
            str(report_path),
        ],
    )
    with pytest.raises(KeyboardInterrupt):
        cold_graph_check.main()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["exception_type"] == "KeyboardInterrupt"
    assert report["complete"] is False
    assert report["errors"]
    assert not list(tmp_path.glob(f".{report_path.name}.*.tmp"))


def test_ingest_sha256_returns_digest(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"release payload")

    assert _sha256_file(path) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_cold_graph_main_writes_atomic_failed_report_on_cancelled_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"sources": [{"source_id": "one"}]}), encoding="utf-8")
    report_path = tmp_path / "cold-cancelled.json"

    async def stop_run(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(cold_graph_check, "_check", stop_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cold_graph_check.py",
            "--workspace",
            str(tmp_path / "workspace"),
            "--manifest",
            str(manifest),
            "--report",
            str(report_path),
        ],
    )

    with pytest.raises(asyncio.CancelledError):
        cold_graph_check.main()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["exception_type"] == "CancelledError"
    assert report["complete"] is False
    assert not list(tmp_path.glob(f".{report_path.name}.*.tmp"))


def test_security_scan_blocks_staged_env_even_without_content_finding(tmp_path: Path) -> None:
    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path),
        patch.object(security_release_scan, "_candidate_files", return_value=[]),
        patch.object(security_release_scan, "_git_file_names", side_effect=[{".env"}, {".env"}]),
    ):
        report = security_release_scan.scan()
    assert report["tracked_protected_files"] == [".env"]
    assert report["staged_protected_files"] == [".env"]


def test_sync_media_contract_fails_closed_on_missing_card(tmp_path: Path) -> None:
    normalized = tmp_path / "normalized" / "Channel"
    normalized.mkdir(parents=True)
    (normalized / "1.meta.json").write_text("{}", encoding="utf-8")
    report = sync_media_contract(normalized, tmp_path / "enriched")
    assert report["status"] == "failed"
    assert report["errors"]


def test_sync_media_contract_dry_run_reports_changes_pending(tmp_path: Path) -> None:
    normalized = tmp_path / "normalized" / "Channel"
    enriched = tmp_path / "enriched" / "Channel"
    normalized.mkdir(parents=True)
    enriched.mkdir(parents=True)
    (normalized / "1.meta.json").write_text(
        json.dumps(
            {
                "media": [
                    {
                        "media_type": "video",
                        "message_id": "1",
                        "download_status": "downloaded",
                        "transcription_status": "failed",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (enriched / "1.enriched.json").write_text(
        json.dumps(
            {
                "schema_version": "enriched_v2",
                "prompt_version": "test",
                "enrichment_model": "test-model",
                "enriched_at": "2026-01-01T00:00:00+00:00",
                "provenance": {"source_id": "source:1"},
            }
        ),
        encoding="utf-8",
    )
    report = sync_media_contract(normalized, enriched, apply=False)
    assert report["status"] == "changes_pending"
    assert report["changes_pending"] is True
    assert report["downstream_rebuild_required"] is False


def test_sync_media_contract_skips_dedicated_youtube_only_metadata_without_card(
    tmp_path: Path,
) -> None:
    normalized = tmp_path / "normalized" / "Балтийские страны"
    normalized.mkdir(parents=True)
    (normalized / "2.meta.json").write_text(
        json.dumps(
            {
                "media": [],
                "youtube_urls": ["https://www.youtube.com/watch?v=video"],
                "youtube_sources": [{"video_id": "video", "has_transcript": True}],
            }
        ),
        encoding="utf-8",
    )
    report = sync_media_contract(normalized, tmp_path / "enriched")
    assert report["status"] == "passed"
    assert report["errors"] == []
    assert report["candidates"] == 0


def test_verifier_rejects_empty_manifest_and_zero_counts() -> None:
    errors: list[str] = []
    assert _manifest_sources({"schema_version": "accepted_manifest_v2", "sources": []}, errors) == []
    assert errors
    assert not _positive_int(0)
    assert not _positive_int("1")


def test_verifier_requires_ten_unique_late_fusion_cases() -> None:
    cases = [
        {
            **spec,
            "pipeline": "late_fusion",
                "fallback": False,
            "errors": [],
            "citation_count": 1,
            "reference_count": 1,
            "youtube_start_url_count": 0,
        }
        for spec in official_cases_payload()
    ]
    references = [{"reference_id": "S1", "url": "https://example.test/source"}]
    for case in cases:
        case["references"] = references
        case["citation_ids"] = ["S1"]
    attempt = {
        "attempt_id": "attempt-001",
        "attempt_index": 1,
        "started_at": "2026-08-18T00:00:00+00:00",
        "finished_at": "2026-08-18T00:00:01+00:00",
        "previous_attempt_sha256": None,
    }
    attempt.update({"passed": True, "case_count": 10, "errors": []})
    cases_sha256 = release_identity.sha256_bytes(release_identity.canonical_json(cases))
    attempt["cases_sha256"] = cases_sha256
    attempt["attempt_sha256"] = release_identity.sha256_bytes(
        release_identity.canonical_json(attempt)
    )
    payload = {"cases": cases, "cases_sha256": cases_sha256, "official_cases_sha256": OFFICIAL_CASES_SHA256, "attempt_id": "attempt-001", "attempts": [attempt]}
    assert _smoke_is_strict(payload)
    cases[-1]["pipeline"] = "legacy"
    assert not _smoke_is_strict({"cases": cases})
