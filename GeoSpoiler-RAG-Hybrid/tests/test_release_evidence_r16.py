from __future__ import annotations

import ast
import asyncio
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts import (
    build_release_evidence,
    build_release_manifest,
    build_release_verification,
    late_fusion_activation_smoke,
    release_identity,
)
from tests.test_release_evidence_r15 import _unsafe_exception_renderings


def _secret(letter: str) -> str:
    return "sk-or-v1-" + letter * 24


def test_manifest_producer_self_validates_nonempty_payload(tmp_path: Path) -> None:
    normalized = tmp_path / "normalized" / "one.txt"
    enriched = tmp_path / "enriched"
    card = enriched / "one.enriched.json"
    normalized.parent.mkdir()
    enriched.mkdir()
    normalized.write_text("source", encoding="utf-8")
    card.write_text(
        json.dumps(
            {
                "schema_version": "enriched_v2",
                "prompt_version": "test-v1",
                "enrichment_model": "test-model",
                "enriched_at": "2026-08-25T00:00:00+00:00",
                "graph_text": "Entity: one\nRelation: source",
                "provenance": {
                    "source_id": "fixture:one",
                    "normalized_path": str(normalized.relative_to(tmp_path)),
                },
                "quality_flags": [],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "accepted.json"
    with (
        patch.object(build_release_manifest.config, "PROJECT_ROOT", tmp_path),
        patch.object(build_release_manifest.config, "ENRICHED_DIR", enriched),
        patch.object(build_release_manifest.config, "NORMALIZED_DIR", normalized.parent),
        patch.object(build_release_manifest.config, "YOUTUBE_NORMALIZED_DIR", tmp_path / "youtube"),
        patch.object(build_release_manifest.llm_backend, "active_model_for", return_value="test-model"),
        patch.object(
            build_release_manifest,
            "validate_manifest_payload",
            wraps=build_release_manifest.validate_manifest_payload,
        ) as validator,
    ):
        result = build_release_manifest.build(output)
        payload = release_identity.strict_json_loads(output.read_bytes())
        assert result["sources"] == 1
        validator.assert_called_once_with(payload)
        assert build_release_manifest.validate_manifest_payload(payload).errors == []


def test_manifest_validation_failure_blocks_publication(tmp_path: Path) -> None:
    output = tmp_path / "accepted.json"
    enriched = tmp_path / "enriched"
    normalized = tmp_path / "normalized" / "one.txt"
    enriched.mkdir()
    normalized.parent.mkdir()
    normalized.write_text("source", encoding="utf-8")
    (enriched / "one.enriched.json").write_text("{}", encoding="utf-8")
    card = SimpleNamespace(
        provenance=SimpleNamespace(
            source_id="fixture:one",
            normalized_path=str(normalized.relative_to(tmp_path)),
        ),
        quality_flags=[],
        graph_text="Entity: one",
    )
    with (
        patch.object(build_release_manifest.config, "PROJECT_ROOT", tmp_path),
        patch.object(build_release_manifest.config, "ENRICHED_DIR", enriched),
        patch.object(build_release_manifest.EnrichedCardV2, "model_validate_json", return_value=card),
        patch.object(
            build_release_manifest,
            "validate_manifest_payload",
            return_value=SimpleNamespace(errors=["mutated required field"]),
        ),
        pytest.raises(RuntimeError),
    ):
        build_release_manifest.build(output)
    assert not output.exists()


def test_manifest_card_path_is_not_rendered_in_public_error(tmp_path: Path) -> None:
    marker = _secret("P")
    enriched = tmp_path / "enriched"
    enriched.mkdir()
    (enriched / f"{marker}.enriched.json").write_text("{}", encoding="utf-8")
    with (
        patch.object(build_release_manifest.config, "ENRICHED_DIR", enriched),
        pytest.raises(RuntimeError) as caught,
    ):
        build_release_manifest.build(tmp_path / "accepted.json")
    assert marker not in "".join(traceback.format_exception(caught.value))


def test_ast_guard_rejects_any_raw_raise_cause() -> None:
    source = """
try:
    work()
except Exception as exc:
    raise RuntimeError('fixed public message') from exc
"""
    assert _unsafe_exception_renderings(source) == [(5, "raw_raise_from")]


def test_live_lock_exception_is_detached_and_does_not_render_hostile_cause(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = _secret("L")
    calls: list[str] = []

    class HostileExists(FileExistsError):
        def __str__(self) -> str:
            calls.append("str")
            print(marker)
            return marker

        def __repr__(self) -> str:
            calls.append("repr")
            return marker

    with (
        patch.object(build_release_evidence.os, "open", side_effect=HostileExists()),
        pytest.raises(FileExistsError) as caught,
    ):
        build_release_evidence.build_bundle(
            tmp_path / "attempt-001", tmp_path / "manifest.json", "run-1", []
        )

    rendered = "".join(traceback.format_exception(caught.value))
    captured = capsys.readouterr()
    assert calls == []
    assert marker not in rendered + captured.out + captured.err
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, asyncio.CancelledError))
def test_late_fusion_smoke_publishes_receipt_then_reraises_interrupt(
    tmp_path: Path, interrupt_type: type[BaseException]
) -> None:
    output = tmp_path / "smoke.json"
    with (
        patch.object(late_fusion_activation_smoke.config, "LATE_FUSION_ENABLED", True),
        patch.object(late_fusion_activation_smoke.config, "RERANKER_ENABLED", False),
        patch.object(
            late_fusion_activation_smoke,
            "create_rag",
            new=AsyncMock(side_effect=interrupt_type),
        ),
        pytest.raises(interrupt_type),
    ):
        asyncio.run(late_fusion_activation_smoke.run_smoke(output))

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert any(interrupt_type.__name__ in error for error in report["errors"])


def test_late_fusion_publication_failure_does_not_replace_interrupt(
    tmp_path: Path,
) -> None:
    with (
        patch.object(late_fusion_activation_smoke.config, "LATE_FUSION_ENABLED", True),
        patch.object(late_fusion_activation_smoke.config, "RERANKER_ENABLED", False),
        patch.object(
            late_fusion_activation_smoke,
            "create_rag",
            new=AsyncMock(side_effect=KeyboardInterrupt),
        ),
        patch.object(
            late_fusion_activation_smoke,
            "atomic_write_release_json",
            side_effect=OSError("safe publication failure"),
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        asyncio.run(late_fusion_activation_smoke.run_smoke(tmp_path / "smoke.json"))


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, asyncio.CancelledError))
def test_final_verifier_publishes_receipt_then_reraises_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_type: type[BaseException],
) -> None:
    run_dir = tmp_path / "attempt-001"
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
        patch.object(build_release_verification, "build_report", side_effect=interrupt_type),
        pytest.raises(interrupt_type),
    ):
        build_release_verification.main()

    report = json.loads(
        (run_dir / "final_release_verification.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "failed"
    assert any(interrupt_type.__name__ in error for error in report["errors"])


def test_registry_covers_direct_release_dependencies() -> None:
    inventory = set(release_identity.RELEASE_EXCEPTION_BOUNDARY_FILES)
    for spec in release_identity.RELEASE_PRODUCER_REGISTRY:
        assert spec.path in inventory
        for dependency in spec.dependencies:
            assert dependency in inventory
            assert Path(dependency).is_file()
    assert "scripts/smoke_contract.py" in inventory
    assert "scripts/security_artifact_redaction.py" in inventory


def test_registry_declares_every_direct_import_inside_release_boundary() -> None:
    inventory = set(release_identity.RELEASE_EXCEPTION_BOUNDARY_FILES)
    registered_schemas = {
        schema
        for spec in release_identity.RELEASE_PRODUCER_REGISTRY
        for schema in spec.schemas
    }
    for spec in release_identity.RELEASE_PRODUCER_REGISTRY:
        tree = ast.parse(Path(release_identity.release_implementation_path(spec.path)).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module in {"scripts", "loader"}:
                    imported.update(
                        f"{node.module}/{alias.name}.py" for alias in node.names
                    )
                elif node.module.startswith(("scripts.", "loader.")):
                    imported.add(node.module.replace(".", "/") + ".py")
                elif node.module.startswith(("geospoiler.operations.", "geospoiler.retrieval.lightrag.")):
                    imported.add("src/" + node.module.replace(".", "/") + ".py")
            elif isinstance(node, ast.Import):
                imported.update(
                    alias.name.replace(".", "/") + ".py"
                    for alias in node.names
                    if alias.name.startswith(("scripts.", "loader."))
                )
        diagnostic_imports = set()
        for relative in imported:
            imported_path = Path(relative)
            if not imported_path.is_file():
                continue
            source = imported_path.read_text(encoding="utf-8")
            if any(
                token in source
                for token in (
                    "safe_exception_diagnostic",
                    "safe_exception_token",
                    "atomic_write_release_json",
                    "atomic_write_release_text",
                    *registered_schemas,
                )
            ):
                diagnostic_imports.add(relative)
        boundary_imports = ((imported & inventory) | diagnostic_imports) - {spec.path}
        assert boundary_imports <= set(spec.dependencies), (
            spec.path,
            sorted(boundary_imports - set(spec.dependencies)),
        )
