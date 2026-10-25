from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import cli_app
import cli_pipeline
from loader import ingest
from scripts import release_identity

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_PRODUCERS = tuple(
    Path(spec.path).name
    for spec in release_identity.RELEASE_PRODUCER_REGISTRY
    if spec.cli
)


def _producer_arguments(filename: str, root: Path) -> list[str]:
    report = root / f"{filename}.report.json"
    manifest = root / "accepted.json"
    spec = next(
        item
        for item in release_identity.RELEASE_PRODUCER_REGISTRY
        if item.cli and Path(item.path).name == filename
    )
    assert spec.cli_probe_args, f"missing registry CLI probe contract: {spec.path}"
    values = {
        "root": root.as_posix(),
        "report": report.as_posix(),
        "manifest": manifest.as_posix(),
    }
    return [argument.format_map(values) for argument in spec.cli_probe_args]


def _secret(letter: str) -> str:
    return "sk-or-v1-" + letter * 24


def _rendered_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


@pytest.mark.parametrize("filename", RELEASE_PRODUCERS)
def test_release_producer_rejects_unknown_argument_without_raw_stderr(
    filename: str,
    tmp_path: Path,
) -> None:
    marker = _secret("A")
    env = {**os.environ, "PYTHONUTF8": "1"}
    sentinel = tmp_path / f"{filename}.report.json"
    sentinel.write_bytes(b"existing-report\n")

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / filename),
            *_producer_arguments(filename, tmp_path),
            f"--unknown-{marker}",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert marker not in result.stdout
    assert marker not in result.stderr
    assert "Traceback" not in result.stderr
    assert "usage:" not in result.stderr.lower()
    diagnostic = json.loads(result.stderr)
    assert diagnostic["status"] == "failed"
    assert diagnostic["error_code"] == "release_cli_argument_rejected"
    assert sentinel.read_bytes() == b"existing-report\n"


@pytest.mark.parametrize(
    ("filename", "arguments"),
    (
        ("migrate_portable_paths.py", lambda marker: [marker, "--report", "safe.json"]),
        (
            "build_release_test_evidence.py",
            lambda marker: ["--output", "safe.json", "--base-ref", marker],
        ),
    ),
)
def test_release_producer_rejects_invalid_choice_without_raw_value(
    filename: str,
    arguments,
) -> None:
    marker = _secret("I")
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / filename), *arguments(marker)],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONUTF8": "1"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert marker not in result.stdout + result.stderr
    assert json.loads(result.stderr)["error_code"] == "release_cli_argument_rejected"


@pytest.mark.parametrize("filename", RELEASE_PRODUCERS)
def test_release_producer_help_remains_available(filename: str) -> None:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / filename), "--help"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONUTF8": "1"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
    assert result.stderr == ""


def test_release_cli_does_not_convert_keyboard_interrupt() -> None:
    def interrupt() -> int:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        release_identity.run_release_cli(interrupt, component="r12_test")


def test_release_cli_discards_secret_bearing_buffered_stderr(capsys) -> None:
    marker = _secret("S")

    def write_stderr() -> int:
        print(marker, file=sys.stderr)
        return 0

    result = release_identity.run_release_cli(write_stderr, component="r12_test")
    captured = capsys.readouterr()

    assert result == 1
    assert marker not in captured.err
    assert json.loads(captured.err)["error_code"] == "release_cli_stderr_rejected"


def test_release_cli_sanitizes_nonzero_system_exit(capsys) -> None:
    marker = _secret("X")

    def exit_nonzero() -> int:
        raise SystemExit(marker)

    result = release_identity.run_release_cli(exit_nonzero, component="r12_test")
    captured = capsys.readouterr()

    assert result == 1
    assert marker not in captured.err
    assert json.loads(captured.err)["error_code"] == "release_cli_exited"


def test_main_load_manifest_path_is_resolved_before_rag_creation() -> None:
    marker = _secret("C")
    create_rag = AsyncMock()

    with (
        patch.object(Path, "resolve", side_effect=OSError(marker)),
        patch.object(cli_pipeline, "create_rag", new=create_rag),
        pytest.raises(Exception) as caught,
    ):
        cli_app.main(["load", "--manifest", "accepted.json"])

    assert marker not in _rendered_exception(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    create_rag.assert_not_awaited()


def test_accepted_loader_resolve_failure_is_detached_and_preflight_only() -> None:
    marker = _secret("R")

    with (
        patch.object(Path, "resolve", side_effect=OSError(marker)),
        patch.object(ingest, "load_manifest_file") as manifest_reader,
        patch.object(ingest, "_write_accepted_manifest_report") as report_writer,
        patch.object(ingest, "load_texts") as loader,
        pytest.raises(Exception) as caught,
    ):
        asyncio.run(ingest.load_from_manifest(object(), Path("accepted.json")))

    assert marker not in str(caught.value)
    assert marker not in repr(caught.value)
    assert marker not in _rendered_exception(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    manifest_reader.assert_not_called()
    report_writer.assert_not_called()
    loader.assert_not_called()


def test_manifest_and_report_failures_are_both_visible_but_detached() -> None:
    primary_marker = _secret("P")
    report_marker = _secret("W")

    with (
        patch.object(ingest, "load_manifest_file", side_effect=OSError(primary_marker)),
        patch.object(
            ingest,
            "_write_accepted_manifest_report",
            side_effect=ingest.AcceptedManifestReportError(report_marker),
        ),
        pytest.raises(RuntimeError) as caught,
    ):
        asyncio.run(ingest.load_from_manifest(object(), Path("accepted.json")))

    rendered = _rendered_exception(caught.value)
    assert (
        "Cannot read accepted load manifest:accepted_manifest_error:"
        "loader.ingest:OSError:"
    ) in str(caught.value)
    assert "accepted_manifest_report_publication_failed" in str(caught.value)
    assert "AcceptedManifestReportError" in str(caught.value)
    assert primary_marker not in rendered
    assert report_marker not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def _validated_manifest() -> SimpleNamespace:
    snapshot = SimpleNamespace(
        source_id="source-1",
        normalized_path=Path("normalized.txt"),
        graph_text="safe graph text",
        graph_text_sha256="1" * 64,
    )
    return SimpleNamespace(
        payload={"run_id": "run-1", "sources": [{"source_id": "source-1"}]},
        manifest_bytes=b"{}",
        errors=[],
        entries=[{"source_id": "source-1"}],
        snapshots={"source-1": snapshot},
    )


@pytest.mark.parametrize(
    "failure_branch",
    ("shared_validation", "snapshot", "load_texts", "incomplete", "success_report"),
)
def test_each_loader_branch_preserves_primary_and_report_failure(
    failure_branch: str,
) -> None:
    primary_marker = _secret("L")
    report_marker = _secret("Q")
    validation = _validated_manifest()
    if failure_branch == "shared_validation":
        validation.errors = [f"invalid value {primary_marker}"]

    load_result: object = 1
    if failure_branch == "load_texts":
        load_result = ValueError(primary_marker)
    elif failure_branch == "incomplete":
        load_result = 0

    snapshot_errors = (
        [f"snapshot mismatch {primary_marker}"] if failure_branch == "snapshot" else []
    )
    report_calls = 0

    def publish_report(**kwargs):
        nonlocal report_calls
        report_calls += 1
        if failure_branch == "success_report" or kwargs["status"] == "failed":
            raise ingest.AcceptedManifestReportError(report_marker)
        return {"status": "accepted"}

    async def load(*args, **kwargs):
        if isinstance(load_result, BaseException):
            raise load_result
        return load_result

    with (
        patch.object(ingest, "load_manifest_file", return_value=validation),
        patch.object(ingest, "snapshot_mismatches", return_value=snapshot_errors),
        patch.object(ingest, "load_texts", side_effect=load),
        patch.object(ingest, "_write_accepted_manifest_report", side_effect=publish_report),
        pytest.raises(RuntimeError) as caught,
    ):
        asyncio.run(ingest.load_from_manifest(object(), Path("accepted.json")))

    rendered = _rendered_exception(caught.value)
    assert report_calls == 1
    assert "accepted_manifest_report_publication_failed" in str(caught.value)
    assert "AcceptedManifestReportError" in str(caught.value)
    assert primary_marker not in rendered
    assert report_marker not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_failed_report_fallback_preserves_existing_artifact(tmp_path: Path) -> None:
    marker = _secret("F")
    report = tmp_path / "accepted.json"
    existing = b"existing accepted report\n"
    report.write_bytes(existing)

    with (
        patch.object(ingest, "atomic_write_json", side_effect=OSError(marker)),
        pytest.raises(ingest.AcceptedManifestReportError) as caught,
    ):
        ingest._persist_report(report, {}, fallback_payload={"status": "failed"})

    assert report.read_bytes() == existing
    assert marker not in _rendered_exception(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
