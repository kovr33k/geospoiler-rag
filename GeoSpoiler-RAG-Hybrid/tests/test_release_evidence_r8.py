from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from scripts import late_fusion_activation_smoke, security_release_scan
from scripts.build_release_verification import _smoke_is_strict
from scripts.release_identity import (
    canonical_json,
    release_secret_count,
    release_secret_findings,
    release_secret_matches,
    sanitize_release_value,
    scan_release_artifact_tree,
    sha256_bytes,
)


@pytest.mark.parametrize(
    "value,category",
    (
        ("api_key=" + "A" * 24, "credential_assignment"),
        ("authorization=" + "B" * 24, "credential_assignment"),
        ("access_token=" + "C" * 24, "credential_assignment"),
        ("Bearer " + "D" * 24, "bearer_token"),
        ("sk-or-v1-" + "E" * 24, "openrouter_token"),
        ("https://example.test/?api_key=" + "F" * 24, "sensitive_query_value"),
        ("https://example.test/?token=" + "G" * 24, "sensitive_query_value"),
        ("https://example.test/?signature=" + "H" * 24, "sensitive_query_value"),
        ("https://example.test/?safe=1&sig=" + "I" * 24, "sensitive_query_value"),
    ),
)
def test_shared_detector_and_security_scan_cover_every_category(value: str, category: str) -> None:
    assert category in release_secret_findings(value)
    assert release_secret_count(value) == 1
    assert security_release_scan._count_text_findings(value, "fixture.txt") == 1


def test_overlapping_query_and_assignment_are_one_physical_finding() -> None:
    value = "https://example.test/?api_key=" + "J" * 24

    assert release_secret_findings(value) == {
        "credential_assignment": 1,
        "sensitive_query_value": 1,
    }
    assert release_secret_count(value) == 1


def test_exact_allowlist_does_not_hide_adjacent_secret() -> None:
    relative = "tests/test_security_release_scan.py"
    value = "accessToken=secret\napi_key=" + "K" * 24

    assert security_release_scan._count_text_findings(value, relative) == 1


@pytest.mark.parametrize(
    "encoding,bom",
    (
        ("utf-8", False),
        ("utf-8-sig", True),
        ("utf-16", True),
        ("utf-16-le", False),
        ("utf-16-be", False),
    ),
)
def test_artifact_scan_detects_supported_text_encodings(
    tmp_path: Path,
    encoding: str,
    bom: bool,
) -> None:
    token = "sk-or-v1-" + "L" * 24
    (tmp_path / "secret.log").write_bytes(token.encode(encoding))

    result = scan_release_artifact_tree(tmp_path)

    assert result["status"] == "failed"
    assert result["occurrences"] == 1
    assert token not in json.dumps(result)
    if bom:
        assert result["findings"][0]["encodings"] != []


def test_artifact_scan_detects_ascii_secret_inside_binary_bytes(tmp_path: Path) -> None:
    token = "sk-or-v1-" + "M" * 24
    (tmp_path / "secret.bin").write_bytes(b"\x00\xff\x80noise:" + token.encode("ascii"))

    result = scan_release_artifact_tree(tmp_path)

    assert result["status"] == "failed"
    assert result["occurrences"] == 1
    assert token not in json.dumps(result)


def test_raw_ascii_scan_survives_utf16_like_binary_prefix(tmp_path: Path) -> None:
    token = "sk-or-v1-" + "Z" * 24
    prefix = b"\x00A" * 100
    (tmp_path / "mixed.bin").write_bytes(prefix + token.encode("ascii"))

    result = scan_release_artifact_tree(tmp_path)

    assert result["status"] == "failed"
    assert result["occurrences"] == 1
    assert result["findings"][0]["encodings"] == ["binary-latin-1", "utf-8"]


def test_project_security_scan_detects_utf16_assignment(tmp_path: Path) -> None:
    secret = "api_key=" + "Y" * 24
    fixture = tmp_path / "utf16.log"
    fixture.write_bytes(secret.encode("utf-16-le"))
    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path),
        patch.object(security_release_scan, "_candidate_files", return_value=[fixture]),
        patch.object(security_release_scan, "_git_file_names", side_effect=[set(), set()]),
    ):
        report = security_release_scan.scan()

    assert report["status"] == "failed"
    assert report["finding_files"] == 1
    assert report["occurrences"] == 1


def test_match_metadata_and_recursive_sanitization_never_return_secret() -> None:
    token = "sk-or-v1-" + "N" * 24
    matches = release_secret_matches("prefix " + token)
    sanitized, count = sanitize_release_value({token: {"error": "api_key=" + "O" * 24}})

    assert matches
    assert token not in repr(matches)
    assert token not in json.dumps(sanitized)
    assert count == 2


def _smoke_result(secret: str | None = None) -> dict:
    answer = "Clean answer [S1]" if secret is None else f"{secret} [S1]"
    status = {"status": "success", "duration_ms": 1}
    if secret is not None:
        status["details"] = "api_key=" + "P" * 24
    url = "https://example.test/source"
    if secret is not None:
        url += "?token=" + "Q" * 24
    return {
        "llm_response": {"content": answer},
        "data": {
            "references": [
                {
                    "reference_id": "S1",
                    "source_id": "source-1",
                    "url": url,
                    "start_url": "https://example.test/video?t=1",
                }
            ],
            "late_fusion": {
                "pipeline": "late_fusion",
                "channel_statuses": {"lightrag": status},
            },
        },
        "fallback": False,
    }


def test_smoke_redacts_runtime_payload_before_report_and_ledger_write(tmp_path: Path) -> None:
    token = "sk-or-v1-" + "R" * 24
    output = tmp_path / "smoke.json"
    ledger = tmp_path / "smoke_attempts.json"
    with (
        patch.object(late_fusion_activation_smoke.config, "LATE_FUSION_ENABLED", True),
        patch.object(late_fusion_activation_smoke.config, "RERANKER_ENABLED", False),
        patch.object(late_fusion_activation_smoke, "create_rag", new=AsyncMock(return_value=object())),
        patch.object(
            late_fusion_activation_smoke,
            "query_rag_result",
            new=AsyncMock(return_value=_smoke_result(token)),
        ),
        patch.object(
            late_fusion_activation_smoke,
            "_finalize_rag_safely",
            new=AsyncMock(return_value=None),
        ),
    ):
        report = asyncio.run(
            late_fusion_activation_smoke.run_smoke(output, attempt_history_path=ledger)
        )

    published = output.read_text(encoding="utf-8") + ledger.read_text(encoding="utf-8")
    assert token not in published
    assert "api_key=" + "P" * 24 not in published
    assert "?token=" + "Q" * 24 not in published
    assert report["passed"] is False
    assert any(error.startswith("release_secret_material_redacted:count=") for error in report["errors"])
    assert report["cases_sha256"] == sha256_bytes(canonical_json(report["cases"]))
    latest = report["attempts"][-1]
    assert latest["cases_sha256"] == report["cases_sha256"]
    assert latest["attempt_sha256"] == sha256_bytes(
        canonical_json({key: value for key, value in latest.items() if key != "attempt_sha256"})
    )


def test_clean_smoke_payload_keeps_success_behavior(tmp_path: Path) -> None:
    output = tmp_path / "smoke.json"
    with (
        patch.object(late_fusion_activation_smoke.config, "LATE_FUSION_ENABLED", True),
        patch.object(late_fusion_activation_smoke.config, "RERANKER_ENABLED", False),
        patch.object(late_fusion_activation_smoke, "create_rag", new=AsyncMock(return_value=object())),
        patch.object(
            late_fusion_activation_smoke,
            "query_rag_result",
            new=AsyncMock(return_value=_smoke_result()),
        ),
        patch.object(
            late_fusion_activation_smoke,
            "_finalize_rag_safely",
            new=AsyncMock(return_value=None),
        ),
    ):
        report = asyncio.run(late_fusion_activation_smoke.run_smoke(output))

    assert report["passed"] is True
    assert report["errors"] == []


def test_clean_smoke_normalizes_absent_fallback_for_strict_verifier(tmp_path: Path) -> None:
    output = tmp_path / "smoke.json"
    result = _smoke_result()
    result["fallback"] = None
    with (
        patch.object(late_fusion_activation_smoke.config, "LATE_FUSION_ENABLED", True),
        patch.object(late_fusion_activation_smoke.config, "RERANKER_ENABLED", False),
        patch.object(late_fusion_activation_smoke, "create_rag", new=AsyncMock(return_value=object())),
        patch.object(
            late_fusion_activation_smoke,
            "query_rag_result",
            new=AsyncMock(return_value=result),
        ),
        patch.object(
            late_fusion_activation_smoke,
            "_finalize_rag_safely",
            new=AsyncMock(return_value=None),
        ),
    ):
        report = asyncio.run(late_fusion_activation_smoke.run_smoke(output))

    assert all(case["fallback"] is False for case in report["cases"])
    assert _smoke_is_strict(report)


def test_smoke_finalizer_exception_is_published_and_raised_safely(tmp_path: Path) -> None:
    token = "sk-or-v1-" + "W" * 24
    output = tmp_path / "smoke.json"
    ledger = tmp_path / "smoke_attempts.json"
    with (
        patch.object(late_fusion_activation_smoke.config, "LATE_FUSION_ENABLED", True),
        patch.object(late_fusion_activation_smoke.config, "RERANKER_ENABLED", False),
        patch.object(late_fusion_activation_smoke, "create_rag", new=AsyncMock(return_value=object())),
        patch.object(
            late_fusion_activation_smoke,
            "query_rag_result",
            new=AsyncMock(return_value=_smoke_result()),
        ),
        patch.object(
            late_fusion_activation_smoke,
            "_finalize_rag_safely",
            new=AsyncMock(side_effect=RuntimeError(token)),
        ),
        pytest.raises(RuntimeError, match="late_fusion_smoke_failed") as raised,
    ):
        asyncio.run(late_fusion_activation_smoke.run_smoke(output, attempt_history_path=ledger))

    published = output.read_text(encoding="utf-8") + ledger.read_text(encoding="utf-8")
    assert token not in published
    assert token not in str(raised.value)
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is False
