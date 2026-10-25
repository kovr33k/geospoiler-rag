from __future__ import annotations

import json
import traceback
from pathlib import Path
from unittest.mock import patch

import pytest

from loader import ingest
from scripts import release_identity, security_release_scan

_RELEASE_PRODUCER_ENTRYPOINTS = tuple(
    Path(spec.path).name
    for spec in release_identity.RELEASE_PRODUCER_REGISTRY
    if spec.cli
)


def _secret(letter: str) -> str:
    return "sk-or-v1-" + letter * 24


def _rendered_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


def test_accepted_report_fallback_does_not_expose_raw_exception_chain() -> None:
    marker = _secret("E")

    with (
        patch.object(ingest, "atomic_write_json", side_effect=OSError(marker)),
        patch.object(
            ingest,
            "_write_non_atomic_diagnostic",
            side_effect=OSError(marker),
        ),
        pytest.raises(ingest.AcceptedManifestReportError) as caught,
    ):
        ingest._persist_report(Path("unused.json"), {}, fallback_payload={})

    rendered = _rendered_exception(caught.value)
    assert marker not in str(caught.value)
    assert marker not in repr(caught.value)
    assert marker not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "operation",
    (
        lambda: release_identity.preflight_release_identity(
            "release-1", Path("manifest.json")
        ),
        lambda: release_identity.atomic_write_release_json(
            Path("report.json"), {}, label="r11_test"
        ),
        lambda: release_identity.atomic_write_release_text(
            Path("report.md"), "safe", label="r11_test"
        ),
    ),
)
def test_release_path_resolution_failure_has_safe_traceback(operation) -> None:
    marker = _secret("R")

    with (
        patch.object(Path, "resolve", side_effect=OSError(marker)),
        pytest.raises(Exception) as caught,
    ):
        operation()

    rendered = _rendered_exception(caught.value)
    assert marker not in str(caught.value)
    assert marker not in repr(caught.value)
    assert marker not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_release_publication_write_failure_has_safe_traceback() -> None:
    marker = _secret("W")

    with (
        patch.object(
            release_identity,
            "atomic_write_bytes",
            side_effect=OSError(marker),
        ),
        pytest.raises(Exception) as caught,
    ):
        release_identity.atomic_write_release_json(
            Path("report.json"), {}, label="r11_test"
        )

    assert marker not in _rendered_exception(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_release_cli_boundary_emits_only_safe_diagnostic(capsys) -> None:
    marker = _secret("C")

    def fail() -> int:
        raise OSError(marker)

    result = release_identity.run_release_cli(fail, component="r11_test")
    captured = capsys.readouterr()

    assert result == 1
    assert captured.out == ""
    assert marker not in captured.err
    diagnostic = json.loads(captured.err)
    assert diagnostic["status"] == "failed"
    assert diagnostic["error_code"] == "release_cli_failed"
    assert diagnostic["component"] == "r11_test"
    assert diagnostic["exception_type"] == "OSError"


@pytest.mark.parametrize("filename", _RELEASE_PRODUCER_ENTRYPOINTS)
def test_release_producer_entrypoint_uses_safe_cli_boundary(filename: str) -> None:
    source = (Path(__file__).parents[1] / "scripts" / filename).read_text(
        encoding="utf-8"
    )

    assert "run_release_cli(main, component=" in source


def test_scanner_distinguishes_same_display_from_two_physical_roots(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    token = _secret("P")
    project_file = project / "output" / token / "clean.txt"
    external_file = external / token / "clean.txt"
    project_file.parent.mkdir(parents=True)
    external_file.parent.mkdir(parents=True)
    project_file.write_text("clean", encoding="utf-8")
    external_file.write_text("clean", encoding="utf-8")

    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", project),
        patch.object(security_release_scan.config, "OUTPUT_DIR", external),
        patch.object(
            security_release_scan.config,
            "SOURCE_REGISTRY_DB_PATH",
            project / "missing.sqlite",
        ),
        patch.object(
            security_release_scan,
            "_git_file_names",
            side_effect=[set(), set()],
        ),
    ):
        report = security_release_scan.scan()

    assert report["finding_files"] == 2
    assert report["occurrences"] == 2
    assert report["metadata_occurrences"] == 2
    assert {item["path"] for item in report["findings"]} == {
        "output/[REDACTED]/clean.txt"
    }
    assert len({item["path_id"] for item in report["findings"]}) == 2
    serialized = json.dumps(report)
    assert token not in serialized
    assert str(project) not in serialized
    assert str(external) not in serialized


def test_scanner_deduplicates_one_physical_file_seen_through_overlapping_roots(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    token = _secret("D")
    fixture = project / "artifacts" / token / "clean.txt"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("clean", encoding="utf-8")

    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", project),
        patch.object(
            security_release_scan.config,
            "OUTPUT_DIR",
            tmp_path / "missing-output",
        ),
        patch.object(
            security_release_scan.config,
            "SOURCE_REGISTRY_DB_PATH",
            project / "missing.sqlite",
        ),
        patch.object(
            security_release_scan,
            "_git_file_names",
            side_effect=[set(), set()],
        ),
    ):
        candidates = security_release_scan._candidate_files()
        report = security_release_scan.scan()

    assert len(candidates) == 1
    assert report["finding_files"] == 1
    assert report["occurrences"] == 1
    assert report["metadata_occurrences"] == 1
    assert token not in json.dumps(report)


def test_scanner_collision_ids_are_independent_of_candidate_order(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    first_token = _secret("X")
    second_token = _secret("Y")
    first = project / first_token / "clean.txt"
    second = project / second_token / "clean.txt"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("clean", encoding="utf-8")
    second.write_text("clean", encoding="utf-8")

    with patch.object(security_release_scan.config, "PROJECT_ROOT", project):
        candidates = [
            security_release_scan._make_candidate(first, "project", project),
            security_release_scan._make_candidate(second, "project", project),
        ]
        reports = []
        for ordered in (candidates, list(reversed(candidates))):
            with (
                patch.object(
                    security_release_scan,
                    "_candidate_files",
                    return_value=ordered,
                ),
                patch.object(
                    security_release_scan,
                    "_git_file_names",
                    side_effect=[set(), set()],
                ),
            ):
                reports.append(security_release_scan.scan())

    normalized = [
        sorted(report["findings"], key=lambda item: item["path_id"])
        for report in reports
    ]
    assert normalized[0] == normalized[1]
    assert len({item["path_id"] for item in normalized[0]}) == 2
    serialized = json.dumps(reports)
    assert first_token not in serialized
    assert second_token not in serialized
