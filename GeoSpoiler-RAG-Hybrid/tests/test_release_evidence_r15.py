from __future__ import annotations

import ast
import json
import traceback
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import build_release_manifest, cold_graph_check, release_identity


def _secret(letter: str) -> str:
    return "sk-or-v1-" + letter * 24


def _contains_name(node: ast.AST, name: str) -> bool:
    return any(isinstance(item, ast.Name) and item.id == name for item in ast.walk(node))


def _call_contains_name(node: ast.Call, name: str) -> bool:
    return any(_contains_name(arg, name) for arg in node.args) or any(
        _contains_name(keyword.value, name) for keyword in node.keywords
    )


def _unsafe_exception_renderings(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Raise)
            and node.cause is not None
            and not (isinstance(node.cause, ast.Constant) and node.cause.value is None)
        ):
            findings.append((node.lineno, "raw_raise_from"))
    for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler) and node.name):
        name = handler.name
        for node in ast.walk(handler):
            if isinstance(node, ast.FormattedValue) and _contains_name(node.value, name):
                findings.append((node.lineno, "fstring"))
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in {"str", "repr", "format", "print"}:
                    if _call_contains_name(node, name):
                        findings.append((node.lineno, node.func.id))
                elif isinstance(node.func, ast.Attribute) and node.func.attr == "format":
                    if _call_contains_name(node, name):
                        findings.append((node.lineno, "dot_format"))
                elif isinstance(node.func, ast.Attribute) and node.func.attr in {"debug", "info", "warning", "error", "exception", "critical"}:
                    if _call_contains_name(node, name):
                        findings.append((node.lineno, "logger"))
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and _contains_name(node.right, name):
                findings.append((node.lineno, "percent"))
            elif isinstance(node, ast.Attribute) and node.attr == "args" and isinstance(node.value, ast.Name) and node.value.id == name:
                findings.append((node.lineno, "args"))
            elif isinstance(node, ast.Attribute) and node.attr in {"__name__", "__module__", "__qualname__"}:
                if _contains_name(node.value, name):
                    findings.append((node.lineno, "type_metadata"))
    return sorted(set(findings))


def _looks_like_schema_only_release_producer(source: str) -> bool:
    tree = ast.parse(source)
    official_schemas = {
        schema
        for spec in release_identity.RELEASE_PRODUCER_REGISTRY
        for schema in spec.schemas
    } | {
        str(contract["schema_version"])
        for contract in release_identity.REPORT_CONTRACTS.values()
    }
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    byte_literals = {
        bytes(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, bytes)
    }
    decoded_byte_literals = {
        value.decode("utf-8", errors="ignore") for value in byte_literals
    }
    has_schema_key = any(
        isinstance(node, ast.Constant) and node.value == "schema_version"
        for node in ast.walk(tree)
    ) or any("schema_version" in value for value in decoded_byte_literals)
    has_writer = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr
        in {"write_text", "write_bytes", "dump", "dumps"}
        for node in ast.walk(tree)
    )
    has_official_schema = bool(literals & official_schemas) or any(
        schema in value
        for schema in official_schemas
        for value in decoded_byte_literals
    )
    return has_schema_key and has_official_schema and has_writer


def test_ast_guard_covers_rendering_syntax_and_safe_control() -> None:
    unsafe = """
try: pass
except Exception as exc:
    print(str(exc)); print(repr(exc)); print(format(exc)); print(f'{exc!r}')
    print('{}'.format(exc)); print('{value}'.format(value=exc)); print('%s' % exc)
    logger.error('bad', exc); logger.error('bad', exc_info=exc)
    print(exc.args); print(type(exc).__name__); print(exc.__class__.__qualname__)
    raise RuntimeError(f'bad:{exc}') from exc
"""
    safe = """
try: pass
except Exception as exc:
    token = safe_exception_token('failed', exc, component='test')
    logger.error(token)
    raise RuntimeError(token) from None
"""
    assert len(_unsafe_exception_renderings(unsafe)) >= 14
    assert _unsafe_exception_renderings(safe) == []


def test_release_boundary_inventory_is_complete_and_ast_clean() -> None:
    discovered: set[str] = set()
    for root in (
        Path("scripts"), Path("loader"),
        Path("src/geospoiler/operations"), Path("src/geospoiler/retrieval/lightrag"),
    ):
        for path in root.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            schema_producer = _looks_like_schema_only_release_producer(source)
            if schema_producer or any(token in source for token in ("run_release_cli", "atomic_write_release_json", "atomic_write_release_text", "safe_exception_diagnostic", "safe_exception_token")):
                discovered.add(path.as_posix())
    inventory = set(release_identity.RELEASE_EXCEPTION_BOUNDARY_FILES)
    assert discovered <= inventory

    findings: list[str] = []
    for relative in release_identity.RELEASE_EXCEPTION_BOUNDARY_FILES:
        for line, kind in _unsafe_exception_renderings(Path(relative).read_text(encoding="utf-8")):
            findings.append(f"{relative}:{line}:{kind}")
    assert findings == []


def test_schema_only_release_producer_is_discovered() -> None:
    source = """
import json
def main():
    payload = {'schema_version': 'release_evidence_v1'}
    Path('report.json').write_text(json.dumps(payload))
if __name__ == '__main__':
    main()
"""
    assert _looks_like_schema_only_release_producer(source)


def test_cold_checkpoint_error_never_renders_hostile_exception(capsys) -> None:
    marker = _secret("C")
    calls: list[str] = []

    class HostileReadError(OSError):
        def __str__(self) -> str:
            calls.append("str")
            print(marker)
            return marker

        def __repr__(self) -> str:
            calls.append("repr")
            raise RuntimeError(marker)

        def __format__(self, spec: str) -> str:
            calls.append("format")
            print(marker)
            return marker

    with patch.object(Path, "read_text", side_effect=HostileReadError("safe")), pytest.raises(ValueError) as caught:
        cold_graph_check._read_checkpoint(Path("checkpoint.json"))

    captured = capsys.readouterr()
    rendered = "".join(traceback.format_exception(caught.value))
    assert calls == []
    assert marker not in captured.out + captured.err + rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_opaque_type_fingerprint_distinguishes_module_and_qualname() -> None:
    first_type = type("SameOpaque", (), {"__module__": "first_module"})
    second_type = type("SameOpaque", (), {"__module__": "second_module"})
    first = release_identity.safe_exception_diagnostic("r15", ValueError(first_type()), component="r15")
    second = release_identity.safe_exception_diagnostic("r15", ValueError(second_type()), component="r15")
    assert first["message_sha256"] != second["message_sha256"]

    first_nested = type("SameOpaque", (), {"__module__": "same_module", "__qualname__": "First.SameOpaque"})
    second_nested = type("SameOpaque", (), {"__module__": "same_module", "__qualname__": "Second.SameOpaque"})
    nested_first = release_identity.safe_exception_diagnostic("r15", ValueError(first_nested()), component="r15")
    nested_second = release_identity.safe_exception_diagnostic("r15", ValueError(second_nested()), component="r15")
    assert nested_first["message_sha256"] != nested_second["message_sha256"]


def test_opaque_type_fingerprint_is_stable_bounded_and_does_not_call_metaclass_hooks() -> None:
    marker = _secret("T")
    calls: list[str] = []

    class HostileMeta(type):
        def __getattribute__(cls, name: str) -> object:
            calls.append(name)
            raise AssertionError(marker)

    opaque_type = HostileMeta(
        "Opaque",
        (),
        {
            "__module__": marker + "\n" + "m" * 512,
            "__qualname__": marker + "\x00" + "q" * 512,
        },
    )
    first = release_identity.safe_exception_diagnostic("r15", ValueError(opaque_type()), component="r15")
    second = release_identity.safe_exception_diagnostic("r15", ValueError(opaque_type()), component="r15")
    serialized = json.dumps(first, sort_keys=True)
    assert first == second
    assert calls == []
    assert marker not in serialized
    assert "m" * 129 not in serialized
    assert "q" * 129 not in serialized


def test_official_manifest_invalid_card_error_is_safe_and_detached(tmp_path: Path, capsys) -> None:
    marker = _secret("M")
    calls: list[str] = []
    enriched = tmp_path / "enriched"
    enriched.mkdir()
    (enriched / "bad.enriched.json").write_text("{}", encoding="utf-8")

    class HostileCardError(ValueError):
        def __str__(self) -> str:
            calls.append("str")
            print(marker)
            return marker

        def __repr__(self) -> str:
            calls.append("repr")
            raise RuntimeError(marker)

        def __format__(self, spec: str) -> str:
            calls.append("format")
            print(marker)
            return marker

    with (
        patch.object(build_release_manifest.config, "ENRICHED_DIR", enriched),
        patch.object(
            build_release_manifest.EnrichedCardV2,
            "model_validate_json",
            side_effect=HostileCardError("safe"),
        ),
        pytest.raises(RuntimeError) as caught,
    ):
        build_release_manifest.build(tmp_path / "accepted.json")

    captured = capsys.readouterr()
    rendered = "".join(traceback.format_exception(caught.value))
    assert calls == []
    assert marker not in captured.out + captured.err + rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_official_manifest_rejects_empty_corpus_without_publication(tmp_path: Path) -> None:
    enriched = tmp_path / "empty-enriched"
    enriched.mkdir()
    output = tmp_path / "accepted.json"
    with (
        patch.object(build_release_manifest.config, "ENRICHED_DIR", enriched),
        patch.object(build_release_manifest, "config_identity_sha256", return_value="a" * 64),
        patch.object(build_release_manifest.llm_backend, "active_model_for", return_value="test-model"),
    ):
        with pytest.raises(RuntimeError) as caught:
            build_release_manifest.build(output)
    assert str(caught.value) == "release manifest has no accepted sources"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not output.exists()
