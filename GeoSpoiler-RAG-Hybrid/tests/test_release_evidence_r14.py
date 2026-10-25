from __future__ import annotations

import json
import re
import traceback
from pathlib import Path
from unittest.mock import patch

import pytest

from loader import ingest, manifest_contract
from scripts import (
    build_release_test_evidence,
    build_release_verification,
    migrate_portable_paths,
    release_identity,
    security_release_scan,
    sync_media_contract,
    validate_backup_restore,
)


def _secret(letter: str) -> str:
    return "sk-or-v1-" + letter * 24


def test_accepted_loader_write_failure_does_not_publish_raw_exception_type(
    tmp_path: Path,
) -> None:
    marker = _secret("W")
    hostile_error = type(marker, (OSError,), {})("safe")
    report_path = tmp_path / "accepted.json"
    identity = {
        "manifest_sha256": "a" * 64,
        "corpus_identity_sha256": "b" * 64,
    }

    with (
        patch.object(ingest, "_accepted_manifest_report_path", return_value=report_path),
        patch.object(ingest, "atomic_write_json", side_effect=hostile_error),
        patch.object(release_identity, "build_identity", return_value=identity),
        patch.object(
            release_identity,
            "stamp_report",
            side_effect=lambda payload, *args, **kwargs: payload,
        ),
        pytest.raises(ingest.AcceptedManifestReportError) as caught,
    ):
        ingest._write_accepted_manifest_report(
            run_id="run",
            manifest_path=tmp_path / "manifest.json",
            status="accepted",
            expected=1,
            inserted=1,
            skipped=0,
            failed=0,
            errors=[],
            manifest_payload={},
            manifest_bytes=b"{}",
            loaded_sources=[],
        )

    rendered = "".join(traceback.format_exception(caught.value))
    assert report_path.is_file()
    assert marker not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_security_failed_report_is_safe_and_publishable() -> None:
    marker = _secret("S")
    hostile_error = type(marker, (OSError,), {})("safe")

    with patch.object(
        security_release_scan,
        "_candidate_files",
        side_effect=hostile_error,
    ):
        report = security_release_scan.scan()

    serialized = release_identity.canonical_json(report)
    release_identity.assert_no_release_secrets(
        serialized,
        label="security_release_scan_report",
    )
    assert marker.encode("utf-8") not in serialized
    assert report["status"] == "failed"


def test_unordered_primitive_exception_args_have_canonical_fingerprint() -> None:
    first = release_identity.safe_exception_diagnostic(
        "r14_test",
        ValueError({"a": 1, "b": 2}, {1, 2, 3}, frozenset({"x", "y"})),
        component="r14_test",
    )
    second = release_identity.safe_exception_diagnostic(
        "r14_test",
        ValueError({"b": 2, "a": 1}, {3, 2, 1}, frozenset({"y", "x"})),
        component="r14_test",
    )

    assert first["message_sha256"] == second["message_sha256"]


def test_large_unordered_args_use_order_independent_shape() -> None:
    first_dict = {str(index): index for index in range(40)}
    second_dict = {str(index): index for index in reversed(range(40))}
    first = release_identity.safe_exception_diagnostic(
        "r14_test",
        ValueError(first_dict, set(range(40))),
        component="r14_test",
    )
    second = release_identity.safe_exception_diagnostic(
        "r14_test",
        ValueError(second_dict, set(reversed(range(40)))),
        component="r14_test",
    )

    assert first["message_sha256"] == second["message_sha256"]


def test_nested_unordered_fingerprint_is_canonical_and_content_sensitive() -> None:
    first = release_identity.safe_exception_diagnostic(
        "r14_test",
        ValueError({"outer": {"a": 1, "b": 2}, "values": {1, 2}}),
        component="r14_test",
    )
    reordered = release_identity.safe_exception_diagnostic(
        "r14_test",
        ValueError({"values": {2, 1}, "outer": {"b": 2, "a": 1}}),
        component="r14_test",
    )
    different = release_identity.safe_exception_diagnostic(
        "r14_test",
        ValueError({"values": {2, 1}, "outer": {"b": 3, "a": 1}}),
        component="r14_test",
    )

    assert first["message_sha256"] == reordered["message_sha256"]
    assert first["message_sha256"] != different["message_sha256"]


def test_unordered_fingerprint_does_not_compare_hostile_values() -> None:
    calls: list[str] = []

    class HostileValue:
        def __hash__(self) -> int:
            return 1

        def __eq__(self, other: object) -> bool:
            calls.append("eq")
            raise AssertionError("comparison hook called")

        def __lt__(self, other: object) -> bool:
            calls.append("lt")
            raise AssertionError("ordering hook called")

        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("rendering hook called")

    value = HostileValue()
    payload = {value: "safe"}
    calls.clear()

    release_identity.safe_exception_diagnostic(
        "r14_test",
        ValueError(payload),
        component="r14_test",
    )

    assert calls == []


def test_manifest_validator_uses_safe_exception_metadata(tmp_path: Path) -> None:
    marker = _secret("M")
    hostile_error = type(marker, (OSError,), {})("safe")
    with patch.object(Path, "read_bytes", side_effect=hostile_error):
        validation = manifest_contract.load_manifest_file(tmp_path / "manifest.json")

    serialized = json.dumps(validation.errors)
    assert marker not in serialized
    release_identity.assert_no_release_secrets(
        serialized,
        label="manifest_validation_errors",
    )


def test_verifier_and_test_evidence_use_safe_exception_metadata(tmp_path: Path) -> None:
    marker = _secret("V")
    hostile_error = type(marker, (OSError,), {})("safe")
    errors: list[str] = []

    with patch.object(
        build_release_verification,
        "_read_json",
        side_effect=hostile_error,
    ):
        assert build_release_verification._load_json(tmp_path / "evidence.json", errors) == {}

    subprocess_error = build_release_test_evidence._safe_subprocess_error(hostile_error)
    serialized = json.dumps({"errors": errors, "subprocess_error": subprocess_error})
    assert marker not in serialized
    release_identity.assert_no_release_secrets(serialized, label="release_consumer_errors")


def test_media_and_portable_path_consumers_use_safe_exception_metadata(
    tmp_path: Path,
) -> None:
    marker = _secret("P")
    hostile_error = type(marker, (OSError,), {})("safe")
    normalized = tmp_path / "normalized"
    enriched = tmp_path / "enriched"
    normalized.mkdir()
    enriched.mkdir()
    candidate = normalized / "fixture.meta.json"
    candidate.write_text("{}", encoding="utf-8")

    with patch.object(Path, "read_text", side_effect=hostile_error):
        media_report = sync_media_contract.sync_media_contract(normalized, enriched)
    with (
        patch.object(migrate_portable_paths, "_candidate_files", return_value=[candidate]),
        patch.object(Path, "read_text", side_effect=hostile_error),
    ):
        portable_report = migrate_portable_paths.scan()

    serialized = json.dumps(
        {"media": media_report, "portable": portable_report},
        ensure_ascii=False,
    )
    assert marker not in serialized
    release_identity.assert_no_release_secrets(serialized, label="migration_consumer_errors")


def test_backup_restore_uses_safe_exception_metadata(tmp_path: Path) -> None:
    marker = _secret("B")
    hostile_error = type(marker, (ValueError,), {})("safe")
    source = tmp_path / "safe.txt"
    source.write_text("safe", encoding="utf-8")
    manifest = tmp_path / "backup.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "backup_source_manifest_v1",
                "root": str(tmp_path),
                "files": [
                    {
                        "path": source.name,
                        "size": source.stat().st_size,
                        "sha256": release_identity.sha256_file(source),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with patch.object(
        validate_backup_restore,
        "_canonical_restore_path",
        side_effect=hostile_error,
    ), pytest.raises(RuntimeError) as caught:
        validate_backup_restore.validate(manifest, tmp_path / "report.json")

    assert not (tmp_path / "report.json").exists()
    serialized = "".join(traceback.format_exception(caught.value))
    assert marker not in serialized
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    release_identity.assert_no_release_secrets(serialized, label="backup_restore_errors")


def test_release_boundary_has_no_direct_raw_exception_rendering() -> None:
    scope = (
        "loader/ingest.py",
        "loader/manifest_contract.py",
        "scripts/build_release_verification.py",
        "scripts/security_release_scan.py",
        "scripts/build_release_test_evidence.py",
        "scripts/sync_media_contract.py",
        "scripts/validate_backup_restore.py",
        "scripts/migrate_portable_paths.py",
        "scripts/cold_graph_check.py",
    )
    forbidden = re.compile(
        r"type\([^\r\n]+\)\.__name__|\b(?:str|repr)\(exc\)"
    )
    findings: list[str] = []
    for relative in scope:
        source = Path(relative).read_text(encoding="utf-8")
        findings.extend(
            f"{relative}:{match.group(0)}" for match in forbidden.finditer(source)
        )

    assert findings == []
