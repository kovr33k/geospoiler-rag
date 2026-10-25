from __future__ import annotations

import contextlib
import io
import json
import traceback
from pathlib import Path
from unittest.mock import patch

import pytest

from loader import ingest
from scripts import cold_graph_check, release_identity


def _secret(letter: str) -> str:
    return "sk-or-v1-" + letter * 24


def _rendered(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


def _throwing_text_error(marker: str, calls: list[str]) -> BaseException:
    class ThrowingTextError(OSError):
        def __str__(self) -> str:
            calls.append("str")
            raise OSError(marker)

        def __repr__(self) -> str:
            calls.append("repr")
            raise RuntimeError(marker)

    return ThrowingTextError("safe argument")


def test_safe_exception_diagnostic_never_calls_exception_rendering(capsys) -> None:
    marker = _secret("R")
    calls: list[str] = []
    diagnostic = release_identity.safe_exception_diagnostic(
        "release_test_failed",
        _throwing_text_error(marker, calls),
        component="r13_test",
    )
    captured = capsys.readouterr()

    assert calls == []
    assert marker not in json.dumps(diagnostic)
    assert captured.out == ""
    assert captured.err == ""


def test_exception_rendering_side_effects_are_not_invoked(capsys) -> None:
    marker = _secret("S")
    calls: list[str] = []

    class SideEffectError(RuntimeError):
        def __str__(self) -> str:
            calls.append("str")
            print(marker)
            return marker

        def __repr__(self) -> str:
            calls.append("repr")
            print(marker, file=__import__("sys").stderr)
            return marker

    diagnostic = release_identity.safe_exception_diagnostic(
        "release_test_failed", SideEffectError("safe"), component="r13_test"
    )
    captured = capsys.readouterr()

    assert calls == []
    assert marker not in json.dumps(diagnostic)
    assert captured.out == ""
    assert captured.err == ""


def test_exception_args_override_is_not_invoked() -> None:
    marker = _secret("A")
    calls: list[str] = []

    class HostileArgsError(RuntimeError):
        @property
        def args(self):
            calls.append("args")
            raise RuntimeError(marker)

    exc = HostileArgsError.__new__(HostileArgsError)
    BaseException.__init__(exc, "safe")

    diagnostic = release_identity.safe_exception_diagnostic(
        "release_test_failed", exc, component="r13_test"
    )

    assert calls == []
    assert marker not in json.dumps(diagnostic)


def test_nested_rendering_failure_does_not_replace_diagnostic() -> None:
    marker = _secret("N")
    calls: list[str] = []

    class NestedFailureError(RuntimeError):
        def __str__(self) -> str:
            calls.append("str")
            try:
                raise OSError(marker)
            except OSError as nested:
                raise RuntimeError(marker) from nested

    diagnostic = release_identity.safe_exception_diagnostic(
        "release_test_failed", NestedFailureError("safe"), component="r13_test"
    )

    assert calls == []
    assert marker not in json.dumps(diagnostic)


def test_diagnostic_preserves_real_keyboard_interrupt(monkeypatch) -> None:
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        release_identity,
        "_encode_exception_fingerprint_value",
        interrupt,
    )

    with pytest.raises(KeyboardInterrupt):
        release_identity.safe_exception_diagnostic(
            "release_test_failed", ValueError("safe"), component="r13_test"
        )


def test_safe_message_fingerprint_distinguishes_primitive_args() -> None:
    first = release_identity.safe_exception_diagnostic(
        "release_test_failed", ValueError("first"), component="r13_test"
    )
    second = release_identity.safe_exception_diagnostic(
        "release_test_failed", ValueError("second"), component="r13_test"
    )

    assert first["message_sha256"] != second["message_sha256"]


def test_safe_message_fingerprint_uses_opaque_type_tag() -> None:
    class FirstOpaque:
        pass

    class SecondOpaque:
        pass

    first = release_identity.safe_exception_diagnostic(
        "release_test_failed", ValueError(FirstOpaque()), component="r13_test"
    )
    second = release_identity.safe_exception_diagnostic(
        "release_test_failed", ValueError(SecondOpaque()), component="r13_test"
    )

    assert first["message_sha256"] != second["message_sha256"]


def test_safe_exception_diagnostic_does_not_render_hostile_argument(capsys) -> None:
    marker = _secret("H")
    calls: list[str] = []

    class HostileArgument:
        def __str__(self) -> str:
            calls.append("str")
            print(marker)
            return marker

        def __repr__(self) -> str:
            calls.append("repr")
            print(marker, file=__import__("sys").stderr)
            return marker

        def __iter__(self):
            calls.append("iter")
            raise RuntimeError(marker)

    cycle: list[object] = []
    cycle.append(cycle)
    exc = ValueError(HostileArgument(), cycle, {"unicode": "тест"}, b"bytes")

    first = release_identity.safe_exception_diagnostic(
        "release_test_failed", exc, component="r13_test"
    )
    second = release_identity.safe_exception_diagnostic(
        "release_test_failed", exc, component="r13_test"
    )
    captured = capsys.readouterr()

    assert calls == []
    assert first == second
    assert marker not in json.dumps(first)
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize("field", ("error_code", "component", "exception_type"))
def test_safe_exception_diagnostic_rejects_secret_dynamic_fields(field: str) -> None:
    marker = _secret("M")
    error_code = marker if field == "error_code" else "release_test_failed"
    component = marker if field == "component" else "r13_test"
    exception_type = marker if field == "exception_type" else "SafeError"
    error_class = type(exception_type, (Exception,), {})

    diagnostic = release_identity.safe_exception_diagnostic(
        error_code,
        error_class("safe"),
        component=component,
    )

    assert marker not in json.dumps(diagnostic)
    assert set(diagnostic) == {
        "error_code",
        "component",
        "exception_type",
        "message_sha256",
    }


def test_safe_exception_diagnostic_bounds_invalid_type_metadata() -> None:
    invalid_name = "Bad\nType" + "X" * 500
    error_class = type(invalid_name, (Exception,), {})

    diagnostic = release_identity.safe_exception_diagnostic(
        "release_test_failed", error_class("safe"), component="r13_test"
    )

    assert "\n" not in diagnostic["exception_type"]
    assert len(diagnostic["exception_type"]) <= 128


def test_hostile_exception_is_safe_across_shared_consumers(capsys, tmp_path: Path) -> None:
    marker = _secret("C")
    calls: list[str] = []

    public = release_identity.safe_release_exception(
        "release_test_failed",
        _throwing_text_error(marker, calls),
        component="r13_test",
    )
    assert marker not in _rendered(public)
    assert public.__cause__ is None
    assert public.__context__ is None

    with (
        patch.object(
            Path,
            "resolve",
            side_effect=_throwing_text_error(marker, calls),
        ),
        pytest.raises(Exception) as path_error,
    ):
        release_identity.safe_resolve_release_path(
            Path("report.json"), label="r13_path"
        )

    with (
        patch.object(
            release_identity,
            "atomic_write_bytes",
            side_effect=_throwing_text_error(marker, calls),
        ),
        pytest.raises(Exception) as write_error,
    ):
        release_identity.atomic_write_release_json(
            tmp_path / "report.json", {}, label="r13_report"
        )

    cli_stderr = io.StringIO()
    with contextlib.redirect_stderr(cli_stderr):
        exit_code = release_identity.run_release_cli(
            lambda: (_ for _ in ()).throw(_throwing_text_error(marker, calls)),
            component="r13_cli",
        )

    loader_error = ingest._accepted_load_failure(
        "primary safe",
        _throwing_text_error(marker, calls),
    )
    captured = capsys.readouterr()

    assert calls == []
    assert exit_code == 1
    for value in (
        _rendered(path_error.value),
        _rendered(write_error.value),
        _rendered(loader_error),
        cli_stderr.getvalue(),
        captured.out,
        captured.err,
    ):
        assert marker not in value
    for error in (path_error.value, write_error.value, loader_error):
        assert error.__cause__ is None
        assert error.__context__ is None


def test_cold_failure_report_uses_safe_exception_metadata(tmp_path: Path) -> None:
    marker = _secret("K")
    secret_error = type(marker, (Exception,), {})("safe")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    report = cold_graph_check._failed_report(
        workspace=workspace,
        manifest_path=manifest,
        initial_empty_before=True,
        exc=secret_error,
        elapsed_seconds=0.0,
    )
    output = tmp_path / "cold-report.json"
    cold_graph_check._atomic_write_report(output, report)
    serialized = output.read_text(encoding="utf-8")

    assert marker not in serialized
    assert marker not in json.dumps(report)


def test_cold_report_publication_scans_non_release_payload(tmp_path: Path) -> None:
    marker = _secret("P")
    output = tmp_path / "cold-report.json"
    existing = b"existing report\n"
    output.write_bytes(existing)

    with pytest.raises(ValueError):
        cold_graph_check._atomic_write_report(
            output,
            {"status": "failed", "exception_type": marker},
        )

    assert output.read_bytes() == existing


def test_environment_snapshot_does_not_publish_raw_exception_type() -> None:
    marker = _secret("E")
    secret_error = type(marker, (Exception,), {})("safe")

    with patch.object(
        release_identity.importlib_metadata,
        "version",
        side_effect=secret_error,
    ):
        snapshot = release_identity.current_static_environment_snapshot()

    assert marker not in json.dumps(snapshot)


def test_artifact_scan_does_not_publish_raw_read_exception_type(
    tmp_path: Path,
) -> None:
    marker = _secret("D")
    secret_error = type(marker, (OSError,), {})("safe")
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("safe", encoding="utf-8")

    with patch.object(Path, "read_bytes", side_effect=secret_error):
        report = release_identity.scan_release_artifact_tree(tmp_path)

    assert marker not in json.dumps(report)
