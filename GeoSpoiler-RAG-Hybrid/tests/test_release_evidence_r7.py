from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from loader import manifest_contract
from scripts import build_release_risk_register, build_release_verification
from scripts.build_release_evidence import build_bundle
from scripts.build_release_risk_register import validate_risk_register_payload
from scripts.build_release_verification import _test_evidence
from scripts.release_identity import (
    RELEASE_TEST_TIMEOUT_SECONDS,
    TRUSTED_RELEASE_BASE_REF,
    build_identity,
    canonical_json,
    release_git_snapshot,
    release_secret_count,
    scan_release_artifact_tree,
    sha256_bytes,
    sha256_file,
)
from tests.test_release_evidence_contract import (
    _fixture_test_report,
    _release_fixture_reports,
    _valid_manifest,
)


def _release_test_payload(tmp_path: Path) -> tuple[dict, dict]:
    snapshot = release_git_snapshot(TRUSTED_RELEASE_BASE_REF)
    payload = _fixture_test_report(tmp_path)
    payload.update(
        {
            "release_schema_version": "release_evidence_v1",
            "release_identity": {"git_commit": snapshot["head_commit"]},
            "base_ref": snapshot["base_ref"],
            "base_commit": snapshot["base_commit"],
            "merge_base": snapshot["merge_base"],
            "head_commit": snapshot["head_commit"],
            "diff_check_argv": list(snapshot["diff_check_argv"]),
        }
    )
    for check in payload["checks"]:
        if check["name"] == "diff_check":
            check["command_argv"] = list(snapshot["diff_check_argv"])
        check["timeout_seconds"] = RELEASE_TEST_TIMEOUT_SECONDS
    receipts_path = Path(payload["runner_receipts_path"])
    receipts = json.loads(receipts_path.read_text(encoding="utf-8"))
    receipts["gates"]["diff_check"]["command_argv"] = list(snapshot["diff_check_argv"])
    receipts_path.write_text(json.dumps(receipts), encoding="utf-8")
    payload["runner_receipts_sha256"] = sha256_file(receipts_path)
    lifecycle_path = Path(payload["runner_lifecycle_path"])
    lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    lifecycle.update(
        {
            "base_ref": snapshot["base_ref"],
            "commands": {item["name"]: item["command_argv"] for item in payload["checks"]},
            "completed_gates": [item["name"] for item in payload["checks"]],
            "summary_path": payload["summary_path"],
            "receipts_path": payload["runner_receipts_path"],
            "finished_at": "2026-08-18T00:00:01+00:00",
        }
    )
    lifecycle_path.write_text(json.dumps(lifecycle), encoding="utf-8")
    payload["runner_lifecycle_sha256"] = sha256_file(lifecycle_path)
    return payload, snapshot


def _restamp_receipts(payload: dict) -> None:
    receipts_path = Path(payload["runner_receipts_path"])
    receipts = json.loads(receipts_path.read_text(encoding="utf-8"))
    checks = {item["name"]: item for item in payload["checks"]}
    for name, receipt in receipts["gates"].items():
        for field in (
            "command_argv",
            "cwd",
            "started_at",
            "finished_at",
            "duration_ms",
            "timeout_seconds",
            "timed_out",
            "interrupted",
            "exit_code",
            "log_bytes",
            "log_sha256",
        ):
            receipt[field] = checks[name][field]
    receipts_path.write_text(json.dumps(receipts), encoding="utf-8")
    payload["runner_receipts_sha256"] = sha256_file(receipts_path)


def test_release_test_evidence_accepts_current_trusted_git_snapshot(tmp_path: Path) -> None:
    payload, snapshot = _release_test_payload(tmp_path)

    result = _test_evidence(
        payload,
        trusted_git_snapshot=snapshot,
        release_identity=payload["release_identity"],
    )

    assert result["status"] == "passed"


def test_release_test_evidence_rejects_coherent_head_to_head_forgery(tmp_path: Path) -> None:
    payload, snapshot = _release_test_payload(tmp_path)
    forged = ["git", "diff", "--check", "HEAD..HEAD", "--"]
    payload.update(
        {
            "base_ref": "HEAD",
            "base_commit": "a" * 40,
            "merge_base": "b" * 40,
            "head_commit": "c" * 40,
            "diff_check_argv": forged,
        }
    )
    next(item for item in payload["checks"] if item["name"] == "diff_check")["command_argv"] = forged
    _restamp_receipts(payload)

    result = _test_evidence(
        payload,
        trusted_git_snapshot=snapshot,
        release_identity=payload["release_identity"],
    )

    assert result["status"] == "failed"
    assert result["reason"] == "base-aware test identity mismatch"


@pytest.mark.parametrize("tamper", ("cwd", "naive_time", "reversed_time", "timeout", "interrupted"))
def test_release_test_evidence_rejects_untrusted_receipt_fields(tmp_path: Path, tamper: str) -> None:
    payload, snapshot = _release_test_payload(tmp_path)
    check = payload["checks"][0]
    if tamper == "cwd":
        check["cwd"] = str(tmp_path / "elsewhere")
    elif tamper == "naive_time":
        check["started_at"] = "2026-08-18T00:00:00"
    elif tamper == "reversed_time":
        check["started_at"] = "2026-08-18T00:00:02+00:00"
    elif tamper == "timeout":
        check["timeout_seconds"] = RELEASE_TEST_TIMEOUT_SECONDS + 1
    else:
        check["interrupted"] = True
    _restamp_receipts(payload)

    result = _test_evidence(
        payload,
        trusted_git_snapshot=snapshot,
        release_identity=payload["release_identity"],
    )

    assert result["status"] == "failed"


def test_risk_register_rejects_duplicate_json_keys_before_manifest_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "risk.json"
    source.write_text(
        '{"risks":[{"id":"RISK-TOKEN-ROTATION-001","status":"open"}],'
        '"risks":[{"id":"RISK-TOKEN-ROTATION-001","status":"closed",'
        '"completion_basis":"user_attestation"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "risk",
            "--input",
            str(source),
            "--output",
            str(tmp_path / "out.json"),
            "--manifest",
            str(tmp_path / "missing.json"),
            "--release-run-id",
            "run",
        ],
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        build_release_risk_register.main()


def test_risk_register_scans_every_risk_and_rejects_unknown_fields() -> None:
    token = "sk-or-v1-" + ("R" * 24)
    with pytest.raises(ValueError, match="release_secret_material_detected"):
        validate_risk_register_payload(
            {
                "risks": [
                    {
                        "id": "RISK-TOKEN-ROTATION-001",
                        "status": "closed",
                        "completion_basis": "user_attestation",
                    },
                    {"id": "RISK-OTHER-001", "status": "open", "notes": token},
                ]
            }
        )
    with pytest.raises(ValueError, match="unknown=forged"):
        validate_risk_register_payload(
            {
                "risks": [
                    {
                        "id": "RISK-TOKEN-ROTATION-001",
                        "status": "closed",
                        "completion_basis": "user_attestation",
                        "forged": "yes",
                    }
                ]
            }
        )


def test_final_verifier_sanitizes_secret_bearing_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "sk-or-v1-" + ("S" * 24)
    output = tmp_path / "attempt-001"
    monkeypatch.setattr(
        build_release_verification,
        "build_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(token)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify",
            "--input-dir",
            str(tmp_path / "input"),
            "--run-dir",
            str(output),
            "--release-run-id",
            "run",
        ],
    )

    assert build_release_verification.main() == 1

    stdout = capsys.readouterr().out
    artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in output.rglob("*") if path.is_file())
    assert token not in stdout
    assert token not in artifact_text
    report = json.loads((output / "final_release_verification.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["errors"][0].startswith("verifier_runtime_error:RuntimeError:")


def test_exact_artifact_tree_scan_fails_without_returning_secret_value(tmp_path: Path) -> None:
    token = "sk-or-v1-" + ("T" * 24)
    (tmp_path / "runner.log").write_text(token, encoding="utf-8")

    result = scan_release_artifact_tree(tmp_path)

    assert result["status"] == "failed"
    assert result["finding_files"] == 1
    assert token not in json.dumps(result)


def test_shared_secret_detector_allows_explicit_redaction_markers() -> None:
    assert release_secret_count('"api_key":"[REDACTED]"') == 0
    assert release_secret_count("https://example.test/?access_token=%5Bredacted%5D") == 0


def test_bundle_rejects_secret_in_identity_valid_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging, manifest = _valid_manifest(tmp_path)
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    identity = build_identity("release-test", manifest, manifest_payload)
    reports = _release_fixture_reports(staging, manifest, identity)
    risk_path = next(path for kind, path in reports if kind == "risk_register")
    risk = json.loads(risk_path.read_text(encoding="utf-8"))
    risk["risks"].append(
        {
            "id": "RISK-OTHER-001",
            "status": "open",
            "notes": "sk-or-v1-" + ("U" * 24),
        }
    )
    unsigned = {key: value for key, value in risk.items() if key != "payload_sha256"}
    risk["payload_sha256"] = sha256_bytes(canonical_json(unsigned))
    risk_path.write_text(json.dumps(risk), encoding="utf-8")
    monkeypatch.setattr("scripts.build_release_evidence.assert_clean_release_tree", lambda: None)

    with (
        patch.object(manifest_contract.config, "NORMALIZED_DIR", staging / "normalized"),
        patch.object(manifest_contract.config, "YOUTUBE_NORMALIZED_DIR", staging / "normalized"),
        patch.object(manifest_contract.config, "ENRICHED_DIR", staging / "enriched"),
        pytest.raises(ValueError, match="release_secret_material_detected"),
    ):
        build_bundle(
            tmp_path / "attempt-001",
            manifest,
            "release-test",
            reports,
            staging_root=staging,
        )
