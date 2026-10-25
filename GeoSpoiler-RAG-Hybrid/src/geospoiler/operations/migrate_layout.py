"""Copy, verify and switch the local data layout without rewriting source bytes.

Run with the application stopped. The journal contains paths/hashes, never .env
values. Old trees and the original .env are kept under ignored backups/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

MAPPING = {
    "output/normalized": "data/normalized",
    "output/normalized_youtube": "data/normalized_youtube",
    "output/enriched": "data/enriched",
    "output/enriched_segments": "data/enriched_segments",
    "output/review_queue": "data/review_queue",
    "output/transcripts": "data/transcripts",
    "output/visuals": "data/visuals",
    "output/instagram_cache": "data/cache/instagram",
    "output/wiki": "data/wiki/projections",
    "wiki_sidecars": "data/wiki/sidecars",
    "media_cache": "data/cache/media",
    "state": "data/state",
    "rag_storage": "data/indexes/lightrag",
    "artifacts/card_fts.sqlite": "data/indexes/card_fts.sqlite",
    "artifacts/source_registry.sqlite": "data/indexes/source_registry.sqlite",
    "artifacts/wiki_state.sqlite": "data/wiki/state.sqlite",
}
SETTINGS = {
    "GEOSPOILER_DATA_LAYOUT": "2", "WIKI_ENABLED": "false", "HYBRID_QUERY_WIKI_ENABLED": "false",
    "OUTPUT_DIR": "./data", "YOUTUBE_NORMALIZED_DIR": "./data/normalized_youtube",
    "YOUTUBE_SEGMENTS_DIR": "./data/enriched_segments", "RAG_STORAGE_DIR": "./data/indexes/lightrag",
    "CARD_FTS_DB_PATH": "./data/indexes/card_fts.sqlite", "SOURCE_REGISTRY_DB_PATH": "./data/indexes/source_registry.sqlite",
    "WIKI_STATE_DB_PATH": "./data/wiki/state.sqlite", "WIKI_OUTPUT_DIR": "./data/wiki/projections",
    "WIKI_SIDECAR_DIR": "./data/wiki/sidecars", "STATE_DIR": "./data/state",
    "CODEX_RUNTIME_DIR": "./data/state/codex_runtime", "MEDIA_CACHE_DIR": "./data/cache/media",
    "INSTAGRAM_CACHE_DIR": "./data/cache/instagram",
}
JOURNAL = "backups/structure-layout/migration.json"


def _safe(root: Path, relative: str) -> Path:
    value = root / relative
    value.resolve().relative_to(root.resolve())
    current = value
    while current != root:
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("Migration does not follow links: " + relative)
        if current == current.parent:
            raise ValueError("Path outside migration root")
        current = current.parent
    return value


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def plan(root: Path) -> dict:
    root = root.resolve()
    records = []
    entries = []
    for old, new in MAPPING.items():
        source = _safe(root, old)
        _safe(root, new)
        if not source.exists():
            continue
        if source.is_file():
            for suffix in ("-wal", "-shm", "-journal"):
                sibling = source.with_name(source.name + suffix)
                if sibling.exists() and sibling.stat().st_size:
                    raise ValueError("Close/checkpoint SQLite before migration: " + old)
        entries.append({"old": old, "new": new})
        # No live SQLite family is safe for a byte-for-byte rename snapshot.
        # Callers must stop writers/checkpoint first, or supply a SQLite backup.
        candidates = [source] if source.is_file() else list(source.rglob("*"))
        for path in candidates:
            _safe(root, path.relative_to(root).as_posix())
            if not path.is_file():
                continue
            if path.name.endswith(("-wal", "-shm", "-journal")) and path.stat().st_size:
                raise ValueError("SQLite writer/journal must be closed before migration: " + str(path.relative_to(root)))
            suffix = "" if source.is_file() else path.relative_to(source).as_posix()
            target = new + ("/" + suffix if suffix else "")
            records.append({"old": path.relative_to(root).as_posix(), "new": target,
                            "bytes": path.stat().st_size, "sha256": _digest(path)})
    return {"version": 2, "identity_root": str(root), "phase": "planned", "entries": entries,
            "files": records, "bytes": sum(row["bytes"] for row in records), "moved": []}


def verify(root: Path, record: dict, *, sources: bool = True) -> None:
    for key in (("old", "new") if sources else ("new",)):
        expected = {row[key] for row in record["files"]}
        actual = set()
        for entry in record["entries"]:
            location = _safe(root, entry[key])
            for path in ([location] if location.is_file() else location.rglob("*")):
                if path.is_file():
                    actual.add(path.relative_to(root).as_posix())
        if actual != expected:
            raise ValueError("Migration inventory changed: " + key)
    for row in record["files"]:
        for key in (("old", "new") if sources else ("new",)):
            path = _safe(root, row[key])
            if not path.is_file() or path.stat().st_size != row["bytes"] or _digest(path) != row["sha256"]:
                raise ValueError("Migration content mismatch: " + row[key])


def stage(root: Path) -> dict:
    root = root.resolve()
    journal = _safe(root, JOURNAL)
    if journal.exists():
        previous = json.loads(journal.read_text(encoding="utf-8"))
        if previous["phase"] == "active":
            verify(root, previous, sources=False)
            return previous
        if previous["phase"] not in {"planned", "staged", "rolled_back"}:
            raise ValueError("Recover the interrupted activation before staging")
    record = plan(root)
    _write(journal, record)
    for row in record["files"]:
        source, target = _safe(root, row["old"]), _safe(root, row["new"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if _digest(target) != row["sha256"]:
                raise ValueError("Conflicting migration target: " + row["new"])
        else:
            shutil.copy2(source, target)
    verify(root, record)
    _write(root / "data/.layout-v2.json", {"version": 2, "identity_root": record["identity_root"]})
    record["phase"] = "staged"
    _write(journal, record)
    return record


def _settings_text(original: str) -> str:
    seen = set()
    output = []
    for line in original.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in SETTINGS:
            if key not in seen:
                output.append(key + "=" + SETTINGS[key])
                seen.add(key)
        else:
            output.append(line)
    output.extend(key + "=" + value for key, value in SETTINGS.items() if key not in seen)
    return "\n".join(output) + "\n"


def activate(root: Path) -> dict:
    root = root.resolve()
    journal = _safe(root, JOURNAL)
    record = json.loads(journal.read_text(encoding="utf-8"))
    if record["phase"] == "active":
        verify(root, record, sources=False)
        return record
    if record["phase"] != "staged":
        raise ValueError("Stage and verify before activation")
    verify(root, record)
    env = _safe(root, ".env")
    backup = _safe(root, "backups/structure-layout/original.env")
    if not backup.exists():
        shutil.copy2(env, backup)
    elif _digest(backup) != _digest(env):
        raise ValueError("Original .env changed; preserve a fresh rollback snapshot first")
    record["env_sha256"] = _digest(env)
    record["phase"] = "activating"
    _write(journal, record)
    for entry in record["entries"]:
        source = _safe(root, entry["old"])
        target = _safe(root, "backups/structure-layout/legacy/" + entry["old"])
        if target.exists():
            raise ValueError("Rollback destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        record["pending"] = entry["old"]
        _write(journal, record)
        source.rename(target)
        record["moved"].append(entry["old"])
        record.pop("pending", None)
        _write(journal, record)
    temporary = _safe(root, ".env.layout.tmp")
    temporary.write_text(_settings_text(env.read_text(encoding="utf-8-sig")), encoding="utf-8")
    os.replace(temporary, env)
    record["active_env_sha256"] = _digest(env)
    record["phase"] = "active"
    _write(journal, record)
    return record


def rollback(root: Path) -> dict:
    root = root.resolve()
    journal = _safe(root, JOURNAL)
    record = json.loads(journal.read_text(encoding="utf-8"))
    if record["phase"] == "rolled_back":
        return record
    # New writes after activation must be reconciled, never discarded.
    verify(root, record, sources=False)
    moved = list(record["moved"])
    if record.get("pending") and record["pending"] not in moved:
        moved.append(record["pending"])
    for old in reversed(moved):
        source = _safe(root, "backups/structure-layout/legacy/" + old)
        target = _safe(root, old)
        if source.exists():
            if target.exists():
                raise ValueError("Rollback would overwrite newly created data: " + old)
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
    env_backup = _safe(root, "backups/structure-layout/original.env")
    if env_backup.exists():
        if record["phase"] in {"active", "activating"}:
            expected_active = record.get("active_env_sha256")
            if expected_active is None:
                expected_active = hashlib.sha256(
                    _settings_text(env_backup.read_text(encoding="utf-8-sig"))
                    .replace("\n", os.linesep)
                    .encode("utf-8")
                ).hexdigest()
            current_env = _safe(root, ".env")
            current_digest = _digest(current_env)
            if current_digest not in {record.get("env_sha256"), expected_active}:
                raise ValueError("Active .env changed; preserve or reconcile it before rollback")
        shutil.copy2(env_backup, _safe(root, ".env.layout.tmp"))
        os.replace(root / ".env.layout.tmp", root / ".env")
    record.update(phase="rolled_back", moved=[])
    record.pop("pending", None)
    _write(journal, record)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "stage", "activate", "rollback"])
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    result = globals()[args.action](args.root)
    print(json.dumps({"phase": result["phase"], "files": len(result["files"]), "bytes": result["bytes"]}))


if __name__ == "__main__":
    main()
