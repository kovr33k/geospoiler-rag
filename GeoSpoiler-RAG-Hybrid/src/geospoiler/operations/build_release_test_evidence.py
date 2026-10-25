"""Run the release test gates and build identity-bound evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.operations.release_identity import (
    RELEASE_TEST_TIMEOUT_SECONDS,
    STATIC_ENVIRONMENT_PACKAGE_NAMES,
    TRUSTED_RELEASE_BASE_REF,
    SafeArgumentParser,
    assert_no_release_secrets,
    atomic_write_bytes,
    atomic_write_release_json,
    dependency_spec_receipt,
    normalise_distribution_name,
    preflight_release_identity,
    release_git_snapshot,
    run_release_cli,
    safe_exception_diagnostic,
    safe_exception_token,
    safe_resolve_release_path,
    stamp_report,
    static_environment_package_version_errors,
    static_environment_receipt_errors,
    strict_json_loads,
)

_RUNNER_TOKEN = object()
_GATE_NAMES = ("pytest", "ruff", "compileall", "diff_check")
_REQUIRED_PACKAGES = STATIC_ENVIRONMENT_PACKAGE_NAMES


@dataclass(frozen=True)
class _RunnerResult:
    """Private hand-off from the executing runner to the report builder."""

    summary_path: Path
    logs: dict[str, Path]
    exit_codes: dict[str, int]
    receipts: dict[str, dict[str, Any]]
    receipts_path: Path
    lifecycle_path: Path
    token: object


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_summary(path: Path) -> dict[str, Any]:
    payload = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("summary JSON root must be an object")
    modern = {"gates_passed", "gates_failed", "timeouts", "commands"}
    legacy = {"passed", "failed", "skipped", "warnings", "subtests"}
    if not modern.issubset(payload) and not legacy.issubset(payload):
        raise ValueError(f"summary JSON missing fields: {sorted(modern - set(payload))}")
    return payload


def _as_bytes(value: object) -> bytes:
    """Normalize subprocess/mock output before durable receipt publication."""
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8", errors="replace")


def _safe_subprocess_error(exc: BaseException) -> str:
    return safe_exception_token(
        "release_test_subprocess_failed",
        exc,
        component="release_test_runner",
    )


def _pytest_counts(log_path: Path) -> dict[str, Any]:
    """Parse the pytest terminal summary from the runner-owned log."""
    text = log_path.read_text(encoding="utf-8", errors="replace")

    def _count(label: str) -> int | None:
        found = re.search(rf"(\d+)\s+{label}\b", text)
        return int(found.group(1)) if found else None

    passed = _count("passed")
    failed = _count("failed")
    skipped = _count("skipped")
    return {
        "parsed": passed is not None,
        "passed": passed or 0,
        "failed": failed or 0,
        "skipped": skipped or 0,
    }


def _run_environment_command(argv: list[str]) -> dict[str, Any]:
    """Capture a version command without persisting arbitrary command output."""
    try:
        completed = subprocess.run(
            argv,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=60.0,
        )
    except Exception as exc:
        return {
            "argv": list(argv),
            "status": "failed",
            "exit_code": 125,
            "version": None,
            "error": _safe_subprocess_error(exc),
        }
    output = (completed.stdout or completed.stderr or "").strip()
    first_line = output.splitlines()[0].strip()[:160] if output else None
    return {
        "argv": list(argv),
        "status": "passed" if completed.returncode == 0 and first_line else "failed",
        "exit_code": int(completed.returncode),
        "version": first_line,
        "error": None if completed.returncode == 0 and first_line else "command_failed",
    }


def _pip_show_receipt() -> dict[str, Any]:
    parsed: dict[str, str] = {}
    command_receipts: list[dict[str, Any]] = []
    for package in _REQUIRED_PACKAGES:
        argv = [sys.executable, "-m", "pip", "show", package]
        try:
            completed = subprocess.run(
                argv,
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=60.0,
            )
        except Exception as exc:
            command_receipts.append(
                {
                    "package": package,
                    "argv": argv,
                    "status": "failed",
                    "exit_code": 125,
                    "observed_name": None,
                    "observed_version": None,
                    "error": _safe_subprocess_error(exc),
                }
            )
            continue
        package_name: str | None = None
        package_version: str | None = None
        for line in (completed.stdout or "").splitlines():
            if line.startswith("Name: "):
                package_name = normalise_distribution_name(line[6:].strip())
            elif line.startswith("Version: "):
                package_version = line[9:].strip()
        expected_name = normalise_distribution_name(package)
        observed_name_matches = package_name == expected_name
        if package_name and package_version and observed_name_matches:
            parsed[package_name] = package_version
        if completed.returncode != 0:
            error = "command_failed"
        elif not package_name or not package_version:
            error = "package_not_found"
        elif not observed_name_matches:
            error = "observed_name_mismatch"
        else:
            error = None
        command_receipts.append(
            {
                "package": package,
                "argv": argv,
                "status": "passed" if completed.returncode == 0 and package_name and package_version and observed_name_matches else "failed",
                "exit_code": int(completed.returncode),
                "observed_name": package_name,
                "observed_version": package_version,
                "error": error,
            }
        )
    packages = {
        name: parsed.get(normalise_distribution_name(name))
        for name in _REQUIRED_PACKAGES
    }
    missing = [name for name, version in packages.items() if not version]
    version_errors = static_environment_package_version_errors(packages)
    command_failed = any(item["status"] != "passed" for item in command_receipts)
    return {
        "commands": command_receipts,
        "status": "passed" if not missing and not version_errors and not command_failed else "failed",
        "packages": packages,
        "missing_packages": missing,
        "version_errors": version_errors,
        "error": None if not missing and not version_errors and not command_failed else "package_inventory_invalid",
    }


def _dependency_files_receipt() -> dict[str, Any]:
    return dependency_spec_receipt(PROJECT_ROOT)


def _collect_environment_receipt() -> dict[str, Any]:
    """Record the exact static-gate runtime without exposing configuration values."""
    commands = {
        "python": _run_environment_command([sys.executable, "--version"]),
        "pytest": _run_environment_command([sys.executable, "-m", "pytest", "--version"]),
        "ruff": _run_environment_command([sys.executable, "-m", "ruff", "--version"]),
    }
    pip_show = _pip_show_receipt()
    dependency_files = _dependency_files_receipt()
    errors = list(dependency_files["errors"])
    errors.extend(
        f"{name}_version_unavailable"
        for name, receipt in commands.items()
        if receipt["status"] != "passed"
    )
    errors.extend(
        f"missing_package:{name}"
        for name in pip_show.get("missing_packages", [])
    )
    version_errors = static_environment_package_version_errors(
        pip_show.get("packages")
    )
    errors.extend(version_errors)
    if (
        pip_show["status"] != "passed"
        and not pip_show.get("missing_packages")
        and not version_errors
    ):
        errors.append("pip_show_failed")
    receipt = {
        "status": "passed" if not errors else "failed",
        "commands": commands,
        "pip_show": pip_show,
        "dependency_files": dependency_files["files"],
        "dependency_spec_identity_sha256": dependency_files["identity_sha256"],
        "errors": errors,
    }
    errors.extend(static_environment_receipt_errors(receipt))
    receipt["errors"] = sorted(set(errors))
    receipt["status"] = "passed" if not receipt["errors"] else "failed"
    return receipt


def _build_report_from_runner(result: _RunnerResult) -> dict[str, Any]:
    """Build a report only from the runner's private durable hand-off."""
    if not isinstance(result, _RunnerResult) or result.token is not _RUNNER_TOKEN:
        raise ValueError("release test evidence must be built by run_gate")
    if (
        set(result.receipts) != set(_GATE_NAMES)
        or set(result.logs) != set(_GATE_NAMES)
        or set(result.exit_codes) != set(_GATE_NAMES)
    ):
        raise ValueError("runner receipts must contain exactly the four release gates")
    summary_path = result.summary_path.resolve()
    receipts_path = result.receipts_path.resolve()
    lifecycle_path = result.lifecycle_path.resolve()
    if not summary_path.is_file() or not receipts_path.is_file() or not lifecycle_path.is_file():
        raise FileNotFoundError("runner artifacts are incomplete")
    summary = _read_summary(summary_path)
    receipts_payload = strict_json_loads(receipts_path.read_text(encoding="utf-8"))
    if (
        not isinstance(receipts_payload, dict)
        or receipts_payload.get("schema_version") != "release_test_runner_receipts_v1"
        or receipts_payload.get("gates") != result.receipts
    ):
        raise ValueError("runner receipts do not match the executing run")
    lifecycle_payload = strict_json_loads(lifecycle_path.read_text(encoding="utf-8"))
    if (
        not isinstance(lifecycle_payload, dict)
        or lifecycle_payload.get("schema_version") != "release_test_runner_lifecycle_v1"
        or lifecycle_payload.get("status") not in {"passed", "failed"}
        or lifecycle_payload.get("summary_path") != str(summary_path)
        or lifecycle_payload.get("receipts_path") != str(receipts_path)
    ):
        raise ValueError("runner lifecycle does not describe a completed run")
    checks: list[dict[str, Any]] = []
    for name in _GATE_NAMES:
        path = result.logs[name].resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        code = result.exit_codes[name]
        receipt = result.receipts[name]
        if (
            not isinstance(code, int)
            or isinstance(code, bool)
            or not isinstance(receipt, dict)
            or receipt.get("exit_code") != code
            or receipt.get("log_path") != str(path)
            or receipt.get("log_bytes") != path.stat().st_size
            or receipt.get("log_sha256") != _sha256(path)
        ):
            raise ValueError(f"runner receipt does not own gate result: {name}")
        checks.append(
            {
                "name": name,
                "status": "passed" if code == 0 else "failed",
                "exit_code": code,
                **result.receipts[name],
                "log_path": str(path),
                "log_bytes": path.stat().st_size,
                "log_sha256": _sha256(path),
            }
        )
    expected_passed = sum(code == 0 for code in result.exit_codes.values())
    expected_failed = len(_GATE_NAMES) - expected_passed
    expected_timeouts = sum(bool(item.get("timed_out")) for item in result.receipts.values())
    summary_counts = _pytest_counts(result.logs["pytest"])
    if (
        summary.get("commands") != list(_GATE_NAMES)
        or summary.get("gates_passed") != expected_passed
        or summary.get("gates_failed") != expected_failed
        or summary.get("timeouts") != expected_timeouts
        or summary.get("interrupted") != any(bool(item.get("interrupted")) for item in result.receipts.values())
        or summary.get("pytest_counts") != summary_counts
    ):
        raise ValueError("runner summary does not match runner-owned receipts and logs")
    environment = summary.get("environment")
    if not isinstance(environment, dict) or environment.get("status") not in {"passed", "failed"}:
        raise ValueError("runner summary is missing environment receipt")
    if environment.get("status") == "passed":
        environment_errors = static_environment_receipt_errors(environment)
        if environment_errors:
            raise ValueError("runner environment receipt is incomplete: " + "; ".join(environment_errors))
    expected_status = (
        "passed"
        if expected_failed == 0
        and not summary.get("interrupted")
        and environment.get("status") == "passed"
        else "failed"
    )
    if lifecycle_payload.get("status") != expected_status:
        raise ValueError("runner lifecycle status does not match runner-owned results")
    report: dict[str, Any] = {
        "schema_version": "release_test_gate_v1",
        "status": expected_status,
        "summary": summary,
        "summary_path": str(summary_path.resolve()),
        "summary_sha256": _sha256(summary_path.resolve()),
        "execution_mode": "runner_receipt",
        "runner_receipts_path": str(receipts_path),
        "runner_receipts_sha256": _sha256(receipts_path),
        "runner_lifecycle_path": str(lifecycle_path),
        "runner_lifecycle_sha256": _sha256(lifecycle_path),
        "checks": checks,
        "errors": [],
    }
    return report


def run_gate(
    staging_dir: Path,
    *,
    base_ref: str,
    timeout_seconds: float = RELEASE_TEST_TIMEOUT_SECONDS,
) -> tuple[Path, dict[str, Any]]:
    """Execute the four release gates and capture immutable command receipts.

    The lifecycle file is created before the first subprocess.  It is the
    durable failed receipt when the interpreter is interrupted between gates;
    the summary and runner-receipt files are published whenever recovery can
    complete normally.
    """
    staging_dir = staging_dir.resolve()
    if staging_dir.exists():
        if not staging_dir.is_dir():
            raise FileExistsError(f"release test staging path is not a directory: {staging_dir}")
        if any(staging_dir.iterdir()):
            raise FileExistsError(f"release test staging directory must be new and empty: {staging_dir}")
    else:
        staging_dir.mkdir(parents=True, exist_ok=False)
    if base_ref != TRUSTED_RELEASE_BASE_REF:
        raise ValueError(f"untrusted release base ref: {base_ref!r}")
    if timeout_seconds != RELEASE_TEST_TIMEOUT_SECONDS:
        raise ValueError("release test timeout override is not allowed")
    logs_dir = staging_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=False)
    lifecycle_path = staging_dir / "runner_lifecycle.json"
    lifecycle = {
        "schema_version": "release_test_runner_lifecycle_v1",
        "status": "running",
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "base_ref": base_ref,
        "started_at": datetime.now(UTC).isoformat(),
        "completed_gates": [],
        "error": None,
    }
    atomic_write_release_json(lifecycle_path, lifecycle, label="release_test_lifecycle")
    base_commit: str | None = None
    merge_base: str | None = None
    head_commit: str | None = None
    commands = {
        "pytest": [sys.executable, "-m", "pytest", "-q"],
        "ruff": [sys.executable, "-m", "ruff", "check", "."],
        "compileall": [sys.executable, "-m", "compileall", "-q", "."],
        "diff_check": ["git", "diff", "--check"],
    }
    logs: dict[str, Path] = {}
    exit_codes: dict[str, int] = {}
    receipts: dict[str, dict[str, Any]] = {}
    interrupted_error: str | None = None
    pending_interrupt: KeyboardInterrupt | asyncio.CancelledError | None = None
    environment_receipt: dict[str, Any] | None = None
    summary = staging_dir / "summary.json"
    receipts_path = staging_dir / "runner_receipts.json"
    runner_error: BaseException | None = None
    runner_result: tuple[Path, dict[str, Any]] | None = None

    def _update_lifecycle(**updates: Any) -> None:
        lifecycle.update(updates)
        atomic_write_release_json(lifecycle_path, lifecycle, label="release_test_lifecycle")

    def _materialize_missing(reason: str | None) -> None:
        for name, argv in commands.items():
            if name in logs:
                continue
            log_path = logs_dir / f"{name}.log"
            atomic_write_bytes(
                log_path,
                f"NOT RUN: {reason or 'gate did not produce a receipt'}\n".encode(),
            )
            logs[name] = log_path
            exit_codes[name] = 125
            receipts[name] = {
                "command_argv": list(argv),
                "cwd": str(PROJECT_ROOT),
                "exit_code": 125,
                "started_at": None,
                "finished_at": None,
                "duration_ms": 0.0,
                "timeout_seconds": timeout_seconds,
                "timed_out": False,
                "interrupted": True,
            }
            receipts[name].update(
                {
                    "log_path": str(log_path.resolve()),
                    "log_bytes": log_path.stat().st_size,
                    "log_sha256": _sha256(log_path),
                }
            )

    def _publish_runner_artifacts() -> None:
        pytest_counts = _pytest_counts(logs["pytest"])
        environment = environment_receipt or {
            "status": "failed",
            "commands": {},
            "pip_show": {},
            "dependency_files": [],
            "dependency_spec_identity_sha256": None,
            "errors": ["environment_receipt_missing"],
        }
        atomic_write_release_json(
            summary,
            {
                "gates_passed": sum(code == 0 for code in exit_codes.values()),
                "gates_failed": sum(code != 0 for code in exit_codes.values()),
                "timeouts": sum(bool(item.get("timed_out")) for item in receipts.values()),
                "interrupted": interrupted_error is not None,
                "interruption_error": interrupted_error,
                "commands": list(commands),
                "pytest_counts": pytest_counts,
                "environment": environment,
            },
            label="release_test_summary",
        )
        atomic_write_release_json(
            receipts_path,
            {
                "schema_version": "release_test_runner_receipts_v1",
                "runner_pid": os.getpid(),
                "hostname": socket.gethostname(),
                "gates": receipts,
            },
            label="release_test_receipts",
        )

    try:
        git_snapshot = release_git_snapshot(base_ref)
        base_commit = str(git_snapshot["base_commit"])
        merge_base = str(git_snapshot["merge_base"])
        head_commit = str(git_snapshot["head_commit"])
        commands["diff_check"] = list(git_snapshot["diff_check_argv"])
        _update_lifecycle(commands={name: list(argv) for name, argv in commands.items()})
        environment_receipt = _collect_environment_receipt()
        _update_lifecycle(environment_status=environment_receipt["status"])

        for name, argv in commands.items():
            started = datetime.now(UTC)
            timed_out = False
            gate_interrupted = False
            try:
                completed = subprocess.run(
                    argv, cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL,
                    capture_output=True, text=False, check=False, timeout=timeout_seconds
                )
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                completed = subprocess.CompletedProcess(
                    argv,
                    124,
                    stdout=_as_bytes(exc.stdout),
                    stderr=_as_bytes(exc.stderr) + b"\nTIMEOUT\n",
                )
            except BaseException as exc:  # preserve a durable failed receipt on interrupt-style exits
                if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                    pending_interrupt = exc
                diagnostic = safe_exception_diagnostic(
                    "release_test_gate_interrupted",
                    exc,
                    component=f"test_gate:{name}",
                )
                interrupted_error = ":".join(
                    (diagnostic["error_code"], diagnostic["exception_type"], diagnostic["message_sha256"])
                )
                gate_interrupted = True
                completed = subprocess.CompletedProcess(
                    argv,
                    130,
                    stdout=b"",
                    stderr=f"\nINTERRUPTED: {interrupted_error}\n".encode("utf-8", errors="replace"),
                )
            finished = datetime.now(UTC)
            log_path = logs_dir / f"{name}.log"
            if log_path.exists():
                raise FileExistsError(f"test receipt already exists: {log_path}")
            log_bytes = _as_bytes(completed.stdout) + b"\n--- STDERR ---\n" + _as_bytes(completed.stderr)
            try:
                assert_no_release_secrets(log_bytes, label=f"test_log:{name}")
            except ValueError:
                log_bytes = b"RELEASE SECRET MATERIAL DETECTED; LOG CONTENT SUPPRESSED\n"
                completed = subprocess.CompletedProcess(argv, 126, stdout=b"", stderr=b"")
            atomic_write_bytes(
                log_path,
                log_bytes,
            )
            logs[name] = log_path
            exit_codes[name] = int(completed.returncode)
            receipts[name] = {
                "command_argv": list(argv),
                "cwd": str(PROJECT_ROOT),
                "exit_code": int(completed.returncode),
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "duration_ms": (finished - started).total_seconds() * 1000.0,
                "timeout_seconds": timeout_seconds,
                "timed_out": timed_out,
                "interrupted": gate_interrupted,
                "log_path": str(log_path.resolve()),
                "log_bytes": log_path.stat().st_size,
                "log_sha256": _sha256(log_path),
            }
            _update_lifecycle(completed_gates=list(logs), last_gate=name)
            if gate_interrupted:
                break

        _materialize_missing(interrupted_error)
        _publish_runner_artifacts()
        final_status = (
            "passed"
            if all(code == 0 for code in exit_codes.values())
            and not interrupted_error
            and environment_receipt is not None
            and environment_receipt.get("status") == "passed"
            else "failed"
        )
        _update_lifecycle(
            status=final_status,
            finished_at=datetime.now(UTC).isoformat(),
            summary_path=str(summary.resolve()),
            receipts_path=str(receipts_path.resolve()),
        )
        result = _RunnerResult(
            summary_path=summary,
            logs=logs,
            exit_codes=exit_codes,
            receipts=receipts,
            receipts_path=receipts_path,
            lifecycle_path=lifecycle_path,
            token=_RUNNER_TOKEN,
        )
        report = _build_report_from_runner(result)
        report["base_ref"] = base_ref
        report["base_commit"] = base_commit
        report["merge_base"] = merge_base
        report["head_commit"] = head_commit
        report["diff_check_argv"] = commands["diff_check"]
        runner_result = (summary, report)
    except BaseException as exc:
        runner_error = exc
        diagnostic = safe_exception_diagnostic(
            "release_test_runner_failed",
            exc,
            component="release_test_runner",
        )
        error = ":".join(
            (diagnostic["error_code"], diagnostic["exception_type"], diagnostic["message_sha256"])
        )
        try:
            if commands:
                _materialize_missing(error)
                if logs and set(logs) == set(commands):
                    _publish_runner_artifacts()
            _update_lifecycle(
                status="failed",
                finished_at=datetime.now(UTC).isoformat(),
                error=error,
                completed_gates=list(logs),
            )
        except BaseException:
            # The lifecycle path was already attempted at run start.  A
            # publication fault must not erase the original exception.
            pass
    if pending_interrupt is not None:
        raise pending_interrupt
    if runner_error is not None:
        raise runner_error
    if runner_result is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("release test runner produced no result")
    return runner_result


def main() -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Run all release gates; required for CLI use.")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--release-run-id")
    parser.add_argument("--base-ref", required=True, choices=(TRUSTED_RELEASE_BASE_REF,))
    parser.add_argument("--timeout-seconds", type=float, default=RELEASE_TEST_TIMEOUT_SECONDS)
    args = parser.parse_args()
    if not args.execute:
        parser.error("manual exit codes are no longer accepted; use --execute")
    safe_resolve_release_path(args.output, label="release_test_output_path")
    if args.output.exists():
        parser.error("--output must point to a new receipt path")
    preflight = None
    if args.manifest is not None:
        if not args.release_run_id:
            parser.error("--release-run-id is required with --manifest")
        preflight = preflight_release_identity(args.release_run_id, args.manifest)
    _summary, report = run_gate(
        args.output.parent,
        base_ref=args.base_ref,
        timeout_seconds=args.timeout_seconds,
    )
    if preflight is not None:
        identity = preflight.identity
        report = stamp_report(
            report,
            identity,
            "python scripts/build_release_test_evidence.py --execute",
            producer="scripts.build_release_test_evidence",
            producer_schema_version="release_test_gate_v1",
            source_inputs={
                "manifest_sha256": identity["manifest_sha256"],
                "corpus_identity_sha256": identity["corpus_identity_sha256"],
            },
            command_argv=[
                "python", "scripts/build_release_test_evidence.py", "--execute",
                "--base-ref", args.base_ref,
            ],
        )
    atomic_write_release_json(args.output, report, label="release_test_report")
    print(json.dumps({"status": report["status"]}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(run_release_cli(main, component="build_release_test_evidence"))
