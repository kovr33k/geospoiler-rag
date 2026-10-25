from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts import (
    build_release_test_evidence,
    build_release_verification,
    late_fusion_activation_smoke,
    release_identity,
    security_artifact_redaction,
)
from tests.test_release_evidence_r15 import (
    _looks_like_schema_only_release_producer,
    _unsafe_exception_renderings,
)

OFFICIAL_SCHEMAS = tuple(
    sorted(
        {
            schema
            for spec in release_identity.RELEASE_PRODUCER_REGISTRY
            for schema in spec.schemas
        }
        | {
            str(contract["schema_version"])
            for contract in release_identity.REPORT_CONTRACTS.values()
        }
    )
)


def test_ast_guard_rejects_raw_cause_outside_except_handler() -> None:
    source = """
def fail(raw_error):
    raise RuntimeError('public') from raw_error
"""
    assert _unsafe_exception_renderings(source) == [(3, "raw_raise_from")]


def test_ast_guard_rejects_saved_alias_and_allows_from_none() -> None:
    unsafe = """
def fail():
    try:
        work()
    except Exception as exc:
        saved = exc
    def nested():
        raise RuntimeError('public') from saved
    nested()
"""
    safe = """
def fail():
    try: work()
    except Exception:
        raise RuntimeError('public') from None
"""
    assert _unsafe_exception_renderings(unsafe) == [(8, "raw_raise_from")]
    assert _unsafe_exception_renderings(safe) == []


def test_schema_inventory_discovers_every_registered_schema_literal() -> None:
    source = """
import json
def main():
    payload = {'schema_version': 'accepted_manifest_v2'}
    Path('report.json').write_text(json.dumps(payload))
if __name__ == '__main__':
    main()
"""
    assert _looks_like_schema_only_release_producer(source)


@pytest.mark.parametrize("schema", OFFICIAL_SCHEMAS)
def test_schema_inventory_discovers_each_official_schema(schema: str) -> None:
    source = f"""
import json
def main():
    payload = {{'schema_version': {schema!r}}}
    Path('report.json').write_text(json.dumps(payload))
if __name__ == '__main__':
    main()
"""
    assert _looks_like_schema_only_release_producer(source), schema


def test_every_boundary_main_is_a_registered_cli() -> None:
    registered = {
        spec.path for spec in release_identity.RELEASE_PRODUCER_REGISTRY if spec.cli
    }
    registered.update(
        release_identity.release_implementation_path(spec.path)
        for spec in release_identity.RELEASE_PRODUCER_REGISTRY if spec.cli
    )
    mains = set()
    for relative in release_identity.RELEASE_EXCEPTION_BOUNDARY_FILES:
        tree = ast.parse(Path(relative).read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Compare)
            and any(
                isinstance(item, ast.Name) and item.id == "__name__"
                for item in ast.walk(node)
            )
            for node in ast.walk(tree)
        ):
            mains.add(relative)
    assert mains <= registered


def test_registry_cli_metadata_and_schema_ownership_are_complete() -> None:
    owners: dict[str, list[str]] = {}
    for spec in release_identity.RELEASE_PRODUCER_REGISTRY:
        if spec.cli:
            assert spec.cli_probe_args, spec.path
        for schema in spec.schemas:
            owners.setdefault(schema, []).append(spec.path)
    assert {schema: paths for schema, paths in owners.items() if len(paths) != 1} == {}
    assert {
        contract["schema_version"]
        for contract in release_identity.REPORT_CONTRACTS.values()
    } <= set(owners)


def test_security_artifact_redaction_is_registry_driven_safe_cli(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    spec = next(
        spec
        for spec in release_identity.RELEASE_PRODUCER_REGISTRY
        if spec.path == "scripts/security_artifact_redaction.py"
    )
    assert spec.cli
    assert spec.cli_probe_args
    marker = "sk-or-v1-" + "R" * 24
    monkeypatch.setattr(
        sys, "argv", [spec.path, "scan", f"--unknown-{marker}"]
    )
    result = release_identity.run_release_cli(
        security_artifact_redaction.main,
        component="security_artifact_redaction",
    )
    captured = capsys.readouterr()
    assert result != 0
    assert marker not in captured.out + captured.err


@pytest.mark.parametrize("option", ("--root", "--report-dir", "--quarantine-root"))
def test_security_artifact_redaction_rejects_secret_shaped_paths(
    option: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    marker = "sk-or-v1-" + "P" * 24
    monkeypatch.setattr(
        sys,
        "argv",
        ["security_artifact_redaction.py", "scan", option, marker],
    )
    result = release_identity.run_release_cli(
        security_artifact_redaction.main,
        component="security_artifact_redaction",
    )
    captured = capsys.readouterr()
    assert result != 0
    assert marker not in captured.out + captured.err


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, asyncio.CancelledError))
def test_final_verifier_existing_directory_preserves_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_type: type[BaseException],
) -> None:
    run_dir = tmp_path / "attempt-001"
    run_dir.mkdir()
    original = interrupt_type()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_release_verification.py",
            "--input-dir",
            str(tmp_path / "input"),
            "--run-dir",
            str(run_dir),
            "--release-run-id",
            "run-1",
        ],
    )
    with (
        patch.object(build_release_verification, "build_report", side_effect=original),
        pytest.raises(interrupt_type) as caught,
    ):
        build_release_verification.main()
    assert caught.value is original
    failure_dirs = list(tmp_path.glob("attempt-001.failed-*"))
    assert len(failure_dirs) == 1
    assert (failure_dirs[0] / "failed_release_verification.json").is_file()


@pytest.mark.parametrize(
    "secondary",
    ("sanitize_release_value", "_best_effort_verification_failure"),
)
def test_final_verifier_secondary_failure_preserves_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secondary: str,
) -> None:
    original = KeyboardInterrupt()
    run_dir = tmp_path / "attempt-001"
    if secondary == "_best_effort_verification_failure":
        run_dir.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_release_verification.py",
            "--input-dir",
            str(tmp_path / "input"),
            "--run-dir",
            str(run_dir),
            "--release-run-id",
            "run-1",
        ],
    )
    with (
        patch.object(build_release_verification, "build_report", side_effect=original),
        patch.object(
            build_release_verification,
            secondary,
            side_effect=OSError("secondary failure"),
        ),
        pytest.raises(KeyboardInterrupt) as caught,
    ):
        build_release_verification.main()
    assert caught.value is original


@pytest.mark.parametrize(
    "secondary",
    ("smoke_attempt_chain_errors", "assert_no_release_secrets", "stamp_report"),
)
def test_late_smoke_postprocessing_preserves_interrupt(
    tmp_path: Path,
    secondary: str,
) -> None:
    original = KeyboardInterrupt()
    patches = [
        patch.object(late_fusion_activation_smoke.config, "LATE_FUSION_ENABLED", True),
        patch.object(late_fusion_activation_smoke.config, "RERANKER_ENABLED", False),
        patch.object(
            late_fusion_activation_smoke,
            "create_rag",
            new=AsyncMock(side_effect=original),
        ),
        patch.object(
            late_fusion_activation_smoke,
            secondary,
            side_effect=OSError("secondary failure"),
        ),
    ]
    with patches[0], patches[1], patches[2], patches[3], pytest.raises(
        KeyboardInterrupt
    ) as caught:
        asyncio.run(late_fusion_activation_smoke.run_smoke(tmp_path / "smoke.json"))
    assert caught.value is original


def test_late_smoke_stamp_failure_preserves_interrupt(tmp_path: Path) -> None:
    original = KeyboardInterrupt()
    identity = {"manifest_sha256": "a" * 64, "corpus_identity_sha256": "b" * 64}
    with (
        patch.object(late_fusion_activation_smoke.config, "LATE_FUSION_ENABLED", True),
        patch.object(late_fusion_activation_smoke.config, "RERANKER_ENABLED", False),
        patch.object(late_fusion_activation_smoke, "create_rag", new=AsyncMock(side_effect=original)),
        patch.object(late_fusion_activation_smoke, "load_manifest_file", return_value=object()),
        patch.object(
            late_fusion_activation_smoke,
            "preflight_release_identity",
            return_value=SimpleNamespace(identity=identity),
        ),
        patch.object(late_fusion_activation_smoke, "stamp_report", side_effect=OSError("secondary failure")),
        pytest.raises(KeyboardInterrupt) as caught,
    ):
        asyncio.run(
            late_fusion_activation_smoke.run_smoke(
                tmp_path / "smoke.json",
                manifest_path=tmp_path / "accepted.json",
                release_run_id="run-1",
            )
        )
    assert caught.value is original


@pytest.mark.parametrize("gate_index", range(4))
def test_test_gate_reraises_original_interrupt_after_receipts(
    tmp_path: Path, gate_index: int
) -> None:
    original = KeyboardInterrupt()
    snapshot = {
        "base_commit": "a" * 40,
        "merge_base": "b" * 40,
        "head_commit": "c" * 40,
        "diff_check_argv": ["git", "diff", "--check"],
    }
    environment = {
        "status": "passed",
        "commands": {},
        "pip_show": {},
        "dependency_files": [],
        "dependency_spec_identity_sha256": "d" * 64,
        "errors": [],
    }
    calls = 0

    def run_gate_process(argv, **kwargs):
        nonlocal calls
        del kwargs
        if calls == gate_index:
            raise original
        calls += 1
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    with (
        patch.object(build_release_test_evidence, "release_git_snapshot", return_value=snapshot),
        patch.object(build_release_test_evidence, "_collect_environment_receipt", return_value=environment),
        patch.object(build_release_test_evidence.subprocess, "run", side_effect=run_gate_process),
        pytest.raises(KeyboardInterrupt) as caught,
    ):
        build_release_test_evidence.run_gate(
            tmp_path / "runner", base_ref=release_identity.TRUSTED_RELEASE_BASE_REF
        )
    assert caught.value is original


def test_backup_source_and_verification_schemas_are_distinct() -> None:
    schemas = {
        spec.path: set(spec.schemas) for spec in release_identity.RELEASE_PRODUCER_REGISTRY
    }
    assert "backup_source_manifest_v1" in schemas["scripts/luna_reenrich_control.py"]
    assert schemas["scripts/luna_reenrich_control.py"].isdisjoint(
        schemas["scripts/validate_backup_restore.py"]
    )
