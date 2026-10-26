import json
from pathlib import Path

from loader.manifest_contract import validate_manifest_payload
from scripts import build_release_verification
from scripts.build_release_evidence import _safe_staging_file
from scripts.build_release_verification import _evidence_bundle, _smoke_is_strict, _test_evidence
from scripts.release_identity import (
    TRUSTED_RELEASE_BASE_REF,
    canonical_json,
    sha256_bytes,
    strict_json_loads,
    validate_report_envelope,
)


def test_strict_json_loader_rejects_duplicate_keys() -> None:
    try:
        strict_json_loads('{"schema_version":"x","schema_version":"y"}')
    except ValueError as exc:
        assert "duplicate JSON key" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("duplicate keys must be rejected")


def test_report_contract_rejects_unknown_payload_field() -> None:
    payload = {
        "schema_version": "security_release_scan_v2",
        "candidate_files": 1,
        "inventory_sha256": "0" * 64,
        "scanned_files": 1,
        "skipped_files": [], "excluded_roots": [], "binary_files": 0,
        "unreadable_files": 0, "errors": [], "protected_files": [],
        "tracked_protected_files": [], "staged_protected_files": [],
        "git_inspection_errors": [], "finding_files": 0, "occurrences": 0,
        "findings": [], "forged": True,
    }
    assert any(item.startswith("payload_unknown:security:forged") for item in validate_report_envelope(payload, kind="security"))


def test_manifest_contract_rejects_path_outside_corpus_roots(tmp_path: Path, monkeypatch) -> None:
    normalized = tmp_path / "normalized"
    enriched = tmp_path / "enriched"
    normalized.mkdir()
    enriched.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("private", encoding="utf-8")
    monkeypatch.setattr("loader.manifest_contract.config.NORMALIZED_DIR", normalized)
    monkeypatch.setattr("loader.manifest_contract.config.YOUTUBE_NORMALIZED_DIR", normalized)
    monkeypatch.setattr("loader.manifest_contract.config.ENRICHED_DIR", enriched)
    result = validate_manifest_payload({
        "schema_version": "accepted_manifest_v2", "run_id": "r", "pipeline_identity": {},
        "sources": [{"accepted": True, "source_id": "x", "normalized_path": str(external), "card_path": str(external),
                     "normalized_sha256": "0" * 64, "enriched_sha256": "0" * 64, "graph_text_sha256": "0" * 64}],
    })
    assert any("outside allowed directory" in error for error in result.errors)


def test_builder_safe_staging_file_rejects_external_before_read(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    external = tmp_path / "private.txt"
    external.write_text("private", encoding="utf-8")
    try:
        _safe_staging_file(external, staging, "forged log")
    except ValueError as exc:
        assert "escapes staging root" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("external source must be rejected")


def test_evidence_bundle_rejects_unlisted_physical_file(tmp_path: Path) -> None:
    root = tmp_path / "attempt-001"
    root.mkdir()
    manifest = {"schema_version": "release_evidence_v1", "release_identity": {}, "reports": []}
    (root / "evidence_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "unlisted.txt").write_text("not declared", encoding="utf-8")
    errors: list[str] = []
    _evidence_bundle(root, errors)
    assert any("physical_inventory_mismatch" in error for error in errors)


def test_verifier_rejects_fake_exact_release_commands(tmp_path: Path) -> None:
    checks = []
    for name in ("pytest", "ruff", "compileall", "diff_check"):
        log = tmp_path / f"{name}.log"
        log.write_text("ok", encoding="utf-8")
        checks.append({
            "name": name, "status": "passed", "exit_code": 0,
            "command_argv": ["echo", name], "cwd": str(tmp_path),
            "started_at": "2026-08-18T00:00:00+00:00", "finished_at": "2026-08-18T00:00:01+00:00",
            "duration_ms": 1.0, "timed_out": False, "log_path": str(log),
            "log_bytes": log.stat().st_size, "log_sha256": sha256_bytes(log.read_bytes()),
        })
    summary = {"gates_passed": 4, "gates_failed": 0, "timeouts": 0, "commands": [], "pytest_counts": {"parsed": True, "passed": 1, "failed": 0, "skipped": 0}}
    payload = {"release_schema_version": "release_evidence_v1", "status": "passed", "summary": summary,
               "summary_path": str(tmp_path / "summary.json"), "summary_sha256": "0" * 64,
               "checks": checks, "base_ref": TRUSTED_RELEASE_BASE_REF, "base_commit": "a", "merge_base": "b", "head_commit": "c",
               "diff_check_argv": ["git", "diff", "--check", "b..HEAD", "--"]}
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    payload["summary_sha256"] = sha256_bytes(summary_path.read_bytes())
    assert _test_evidence(payload)["status"] == "failed"


def test_smoke_rejects_validly_rehashed_failed_last_attempt() -> None:
    cases = [{
        "id": str(index), "pipeline": "late_fusion", "fallback": False, "errors": [],
        "citation_count": 1, "reference_count": 1, "youtube_start_url_count": 0,
        "references": [{"reference_id": "S1", "url": "https://example.test/source"}], "citation_ids": ["S1"],
    } for index in range(10)]
    cases_sha = sha256_bytes(canonical_json(cases))
    attempt = {"attempt_index": 1, "attempt_id": "attempt-001", "previous_attempt_sha256": None,
               "passed": False, "case_count": 0, "errors": ["boom"], "cases_sha256": cases_sha}
    attempt["attempt_sha256"] = sha256_bytes(canonical_json(attempt))
    assert not _smoke_is_strict({"cases": cases, "cases_sha256": cases_sha, "attempt_id": "attempt-001", "attempts": [attempt]})


def test_verifier_publication_failure_does_not_leave_partial_final_dir(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "attempt-001"
    monkeypatch.setattr(build_release_verification, "build_report", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    original = build_release_verification.atomic_write_json

    def fail_manifest(path: Path, payload):
        if path.name == "verification_manifest.json":
            raise OSError("manifest write fault")
        return original(path, payload)

    monkeypatch.setattr(build_release_verification, "atomic_write_json", fail_manifest)
    monkeypatch.setattr(build_release_verification.sys, "argv", ["verify", "--input-dir", str(tmp_path / "input"), "--run-dir", str(output), "--release-run-id", "run"])
    assert build_release_verification.main() == 1
    assert not output.exists()
    failed_dirs = list(tmp_path.glob("attempt-001.failed-*"))
    assert len(failed_dirs) == 1
    assert (failed_dirs[0] / "verification_manifest.json").is_file()
