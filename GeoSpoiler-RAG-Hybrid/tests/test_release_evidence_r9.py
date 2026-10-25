from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from scripts import late_fusion_activation_smoke, security_release_scan
from scripts.build_release_verification import _smoke_is_strict
from scripts.release_identity import (
    canonical_json,
    release_secret_count,
    scan_release_artifact_tree,
    sha256_bytes,
    validate_manifest_run_id,
)
from scripts.security_artifact_redaction import SQLITE_HEADER
from scripts.smoke_contract import (
    smoke_attempt_chain_errors,
    smoke_attempt_chain_is_valid,
)


def _attempt(index: int, previous_hash: str | None = None) -> dict:
    attempt = {
        "attempt_index": index,
        "attempt_id": f"attempt-{index:03d}",
        "started_at": f"2026-08-{20 + index:02d}T00:00:00+00:00",
        "finished_at": f"2026-08-{20 + index:02d}T00:00:01+00:00",
        "passed": False,
        "case_count": 0,
        "cases_sha256": sha256_bytes(canonical_json([])),
        "errors": ["synthetic_failed_attempt"],
        "previous_attempt_sha256": previous_hash,
    }
    attempt["attempt_sha256"] = sha256_bytes(canonical_json(attempt))
    return attempt


def _clean_smoke_result() -> dict:
    return {
        "llm_response": {"content": "Clean answer [S1]"},
        "data": {
            "references": [
                {
                    "reference_id": "S1",
                    "source_id": "source-1",
                    "url": "https://example.test/source",
                    "start_url": "https://example.test/video?t=1",
                }
            ],
            "late_fusion": {
                "pipeline": "late_fusion",
                "channel_statuses": {
                    "lightrag": {"status": "success", "duration_ms": 1}
                },
            },
        },
        "fallback": False,
    }


@pytest.mark.parametrize("encoding", ("utf-16-le", "utf-16-be"))
@pytest.mark.parametrize(
    "prefix",
    (
        "Привет мир " * 300,
        "漢字かな交じり文" * 300,
        "مرحبا بالعالم " * 300,
    ),
)
def test_exhaustive_utf16_without_bom_detects_secret(
    tmp_path: Path, encoding: str, prefix: str
) -> None:
    token = "sk-or-v1-" + "A" * 24
    payload = (prefix + token).encode(encoding)

    assert release_secret_count(payload) == 1
    (tmp_path / "payload.bin").write_bytes(payload)
    report = scan_release_artifact_tree(tmp_path)
    assert report["status"] == "failed"
    assert report["occurrences"] == 1
    assert token not in json.dumps(report)


def test_byte_representations_do_not_duplicate_one_physical_secret() -> None:
    token = "sk-or-v1-" + "B" * 24
    assert release_secret_count(token.encode("utf-8")) == 1
    assert release_secret_count(("\x00" * 3).encode("latin-1") + token.encode("ascii") + b"\xff") == 1


def test_secret_only_in_path_blocks_both_scanners_without_leaking(tmp_path: Path) -> None:
    token = "sk-or-v1-" + "C" * 24
    fixture = tmp_path / f"{token}.txt"
    fixture.write_text("clean", encoding="utf-8")

    artifact_report = scan_release_artifact_tree(tmp_path)
    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path),
        patch.object(security_release_scan, "_candidate_files", return_value=[fixture]),
        patch.object(security_release_scan, "_git_file_names", side_effect=[set(), set()]),
    ):
        security_report = security_release_scan.scan()

    for report in (artifact_report, security_report):
        serialized = json.dumps(report)
        assert report["status"] == "failed"
        assert report["metadata_occurrences"] == 1
        assert token not in serialized
        assert "[REDACTED]" in serialized


def test_secret_path_and_body_are_both_counted_without_leaking(tmp_path: Path) -> None:
    token = "sk-or-v1-" + "D" * 24
    fixture = tmp_path / f"{token}.txt"
    fixture.write_text(token, encoding="utf-8")

    report = scan_release_artifact_tree(tmp_path)
    assert report["occurrences"] == 2
    assert report["metadata_occurrences"] == 1
    assert token not in json.dumps(report)


def test_secret_error_path_is_sanitized(tmp_path: Path) -> None:
    token = "sk-or-v1-" + "E" * 24
    fixture = tmp_path / f"{token}.sqlite"
    fixture.write_bytes(SQLITE_HEADER + b"broken")
    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path),
        patch.object(security_release_scan, "_candidate_files", return_value=[fixture]),
        patch.object(security_release_scan, "_git_file_names", side_effect=[set(), set()]),
    ):
        report = security_release_scan.scan()

    assert report["status"] == "failed"
    assert report["unreadable_files"] == 1
    assert report["metadata_occurrences"] == 1
    assert token not in json.dumps(report)


def test_artifact_error_path_keeps_metadata_count_without_leaking(
    tmp_path: Path,
) -> None:
    token = "sk-or-v1-" + "I" * 24
    fixture = tmp_path / f"{token}.txt"
    fixture.write_text("clean", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def fail_fixture(path: Path) -> bytes:
        if path == fixture:
            raise OSError("synthetic path-bearing failure")
        return original_read_bytes(path)

    with patch.object(Path, "read_bytes", fail_fixture):
        report = scan_release_artifact_tree(tmp_path)

    assert report["status"] == "failed"
    assert report["metadata_occurrences"] == 1
    assert token not in json.dumps(report)


def test_tracked_protected_path_metadata_is_sanitized(tmp_path: Path) -> None:
    token = "sk-or-v1-" + "F" * 24
    protected = f"private/{token}/.env"
    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path),
        patch.object(security_release_scan, "_candidate_files", return_value=[]),
        patch.object(
            security_release_scan,
            "_git_file_names",
            side_effect=[{protected}, {protected}],
        ),
    ):
        report = security_release_scan.scan()

    assert report["status"] == "failed"
    assert report["metadata_occurrences"] == 1
    assert token not in json.dumps(report)
    assert report["tracked_protected_files"] == ["private/[REDACTED]/.env"]


def test_malformed_sqlite_yields_failed_report_and_scan_continues(tmp_path: Path) -> None:
    malformed = tmp_path / "broken.sqlite"
    clean = tmp_path / "clean.txt"
    malformed.write_bytes(SQLITE_HEADER + b"not-a-database")
    clean.write_text("clean", encoding="utf-8")
    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path),
        patch.object(
            security_release_scan,
            "_candidate_files",
            return_value=[malformed, clean],
        ),
        patch.object(security_release_scan, "_git_file_names", side_effect=[set(), set()]),
    ):
        report = security_release_scan.scan()

    assert report["status"] == "failed"
    assert report["unreadable_files"] == 1
    assert report["scanned_files"] == 1
    assert len(report["errors"]) == 1
    error = report["errors"][0]
    assert error["path"] == "broken.sqlite"
    assert error["error_code"] == "candidate_analysis_failed"
    diagnostic = error["error"].split(":")
    assert diagnostic[:3] == [
        "candidate_analysis_failed",
        "security_release_scan",
        "DatabaseError",
    ]
    assert len(diagnostic[3]) == 64
    int(diagnostic[3], 16)


def test_malformed_sqlite_cli_writes_durable_failed_report(tmp_path: Path) -> None:
    malformed = tmp_path / "broken.sqlite"
    report_path = tmp_path / "security.json"
    malformed.write_bytes(SQLITE_HEADER + b"not-a-database")
    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path),
        patch.object(
            security_release_scan, "_candidate_files", return_value=[malformed]
        ),
        patch.object(security_release_scan, "_git_file_names", side_effect=[set(), set()]),
        patch.object(
            sys,
            "argv",
            ["security_release_scan.py", "--report", str(report_path)],
        ),
    ):
        assert security_release_scan.main() == 1

    published = json.loads(report_path.read_text(encoding="utf-8"))
    assert published["status"] == "failed"
    assert published["unreadable_files"] == 1


def test_manifest_run_id_mismatch_never_echoes_values() -> None:
    actual = "sk-or-v1-" + "G" * 24
    expected = "sk-or-v1-" + "H" * 24
    with pytest.raises(ValueError) as raised:
        validate_manifest_run_id(actual, {"run_id": expected})
    assert str(raised.value) == "manifest_run_id_mismatch"
    assert actual not in str(raised.value)
    assert expected not in str(raised.value)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda item: item.update(attempt_index=True),
        lambda item: item.update(attempt_id="attempt-999"),
        lambda item: item.update(attempt_sha256="0" * 64),
        lambda item: item.update(started_at="2026-08-21T00:00:00"),
        lambda item: item.update(passed=1),
        lambda item: item.update(extra_field=True),
    ),
)
def test_shared_smoke_chain_validator_rejects_forged_attempts(mutation) -> None:
    attempts = [_attempt(1)]
    mutation(attempts[0])
    assert smoke_attempt_chain_errors(attempts)
    assert not smoke_attempt_chain_is_valid(attempts)


@pytest.mark.parametrize("field", ("attempt_id", "previous_attempt_sha256"))
def test_smoke_chain_rejects_duplicate_id_and_broken_previous_hash(field: str) -> None:
    first = _attempt(1)
    second = _attempt(2, first["attempt_sha256"])
    second[field] = "attempt-001" if field == "attempt_id" else "0" * 64
    second["attempt_sha256"] = sha256_bytes(
        canonical_json(
            {key: value for key, value in second.items() if key != "attempt_sha256"}
        )
    )
    assert not smoke_attempt_chain_is_valid([first, second])


def test_smoke_producer_rejects_invalid_ledger_before_rag_and_preserves_bytes(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "smoke_attempts.json"
    output = tmp_path / "smoke.json"
    forged = [_attempt(1)]
    forged[0]["attempt_sha256"] = "0" * 64
    original = canonical_json(forged)
    ledger.write_bytes(original)
    create_rag = AsyncMock(return_value=object())

    with (
        patch.object(late_fusion_activation_smoke.config, "LATE_FUSION_ENABLED", True),
        patch.object(late_fusion_activation_smoke.config, "RERANKER_ENABLED", False),
        patch.object(late_fusion_activation_smoke, "create_rag", new=create_rag),
        pytest.raises(ValueError, match="smoke_attempt_history_invalid"),
    ):
        asyncio.run(
            late_fusion_activation_smoke.run_smoke(
                output, attempt_history_path=ledger
            )
        )

    create_rag.assert_not_awaited()
    assert ledger.read_bytes() == original
    assert not output.exists()


def test_smoke_producer_rejects_duplicate_json_keys_without_rewrite(tmp_path: Path) -> None:
    ledger = tmp_path / "smoke_attempts.json"
    output = tmp_path / "smoke.json"
    original = b'[{"attempt_index":1,"attempt_index":1}]'
    ledger.write_bytes(original)
    with (
        patch.object(late_fusion_activation_smoke.config, "LATE_FUSION_ENABLED", True),
        patch.object(late_fusion_activation_smoke.config, "RERANKER_ENABLED", False),
        patch.object(
            late_fusion_activation_smoke, "create_rag", new=AsyncMock()
        ) as create_rag,
        pytest.raises(ValueError, match="smoke_attempt_history_invalid"),
    ):
        asyncio.run(
            late_fusion_activation_smoke.run_smoke(
                output, attempt_history_path=ledger
            )
        )
    create_rag.assert_not_awaited()
    assert ledger.read_bytes() == original


def test_valid_smoke_chain_is_extended_and_shared_with_verifier(tmp_path: Path) -> None:
    ledger = tmp_path / "smoke_attempts.json"
    output = tmp_path / "smoke.json"
    first = _attempt(1)
    ledger.write_bytes(canonical_json([first]))
    with (
        patch.object(late_fusion_activation_smoke.config, "LATE_FUSION_ENABLED", True),
        patch.object(late_fusion_activation_smoke.config, "RERANKER_ENABLED", False),
        patch.object(
            late_fusion_activation_smoke,
            "create_rag",
            new=AsyncMock(return_value=object()),
        ),
        patch.object(
            late_fusion_activation_smoke,
            "query_rag_result",
            new=AsyncMock(return_value=_clean_smoke_result()),
        ),
        patch.object(
            late_fusion_activation_smoke,
            "_finalize_rag_safely",
            new=AsyncMock(return_value=None),
        ),
    ):
        report = asyncio.run(
            late_fusion_activation_smoke.run_smoke(
                output, attempt_history_path=ledger
            )
        )

    published = json.loads(ledger.read_text(encoding="utf-8"))
    assert len(published) == 2
    assert smoke_attempt_chain_is_valid(published)
    assert _smoke_is_strict(report)
