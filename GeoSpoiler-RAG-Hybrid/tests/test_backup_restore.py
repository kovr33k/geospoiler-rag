import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.validate_backup_restore import validate


def _record(root: Path, relative: str, content: bytes = b"backup") -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {"path": relative, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def _manifest(tmp_path: Path, root: Path, records: list[dict[str, object]]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "backup_source_manifest_v1",
                "root": str(root),
                "files": records,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_repeated_channel_namespace_maps_to_canonical_restore_path(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    relative = "output/enriched/Channel A/Channel A/18.enriched.json"
    report = validate(_manifest(tmp_path, root, [_record(root, relative)]), tmp_path / "report.json")
    assert report["status"] == "passed"
    assert report["nested_paths"] == [relative]
    assert report["canonical_mapping"] == [{"backup_path": relative, "restore_path": "output/enriched/Channel A/18.enriched.json", "rules": ["collapse_repeated_channel_namespace"]}]


def test_repeated_rag_storage_root_maps_to_canonical_restore_path(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    relative = "rag_storage/rag_storage/doc_metadata_index.sqlite"
    report = validate(_manifest(tmp_path, root, [_record(root, relative)]), tmp_path / "report.json")
    assert report["canonical_mapping"][0]["restore_path"] == "rag_storage/doc_metadata_index.sqlite"


def test_traversal_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    root.mkdir()
    record = {"path": "../outside.txt", "bytes": 6, "sha256": hashlib.sha256(b"backup").hexdigest()}
    with pytest.raises(RuntimeError, match="escapes backup root"):
        validate(_manifest(tmp_path, root, [record]), tmp_path / "report.json")


def test_symlink_component_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    root.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (target / "file.txt").write_bytes(b"backup")
    try:
        os.symlink(target, root / "linked", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    record = {"path": "linked/file.txt", "bytes": 6, "sha256": hashlib.sha256(b"backup").hexdigest()}
    with pytest.raises(RuntimeError, match="symlink component is forbidden"):
        validate(_manifest(tmp_path, root, [record]), tmp_path / "report.json")


def test_duplicate_physical_path_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    record = _record(root, "output/normalized/Channel/1.txt")
    with pytest.raises(RuntimeError, match="duplicate path"):
        validate(_manifest(tmp_path, root, [record, dict(record)]), tmp_path / "report.json")


def test_duplicate_canonical_mapping_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    repeated = _record(root, "output/enriched/Channel/Channel/1.json", b"one")
    canonical = _record(root, "output/enriched/Channel/1.json", b"two")
    with pytest.raises(RuntimeError, match="duplicate canonical restore path"):
        validate(_manifest(tmp_path, root, [repeated, canonical]), tmp_path / "report.json")
