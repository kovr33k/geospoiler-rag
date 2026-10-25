"""Fail-closed reconciliation of every live LightRAG provenance path."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geospoiler import config  # noqa: E402
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.operations.release_identity import (
    SafeArgumentParser,
    atomic_write_release_json,
    canonical_json,
    preflight_release_identity,
    run_release_cli,
    safe_resolve_release_path,
    sha256_file,
    stamp_report,
    strict_json_loads,
)
from geospoiler.sources.registry import resolve_source_path  # noqa: E402


def _stable_sqlite_snapshot(path: Path, queries: dict[str, str]) -> tuple[dict[str, list[list[Any]]], dict[str, Any]]:
    """Read all requested tables from one SQLite backup snapshot."""
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(path)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and not sidecar.is_file():
            raise RuntimeError(f"SQLite sidecar is not a regular file: {sidecar}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.snapshot-", suffix=".sqlite", dir=str(path.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        source_uri = f"file:{path.resolve().as_posix()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source, closing(sqlite3.connect(temp_path)) as target:
            source.backup(target)
            target.commit()
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError(f"SQLite backup integrity check failed: {path}")
            rows = {name: [list(row) for row in target.execute(query).fetchall()] for name, query in queries.items()}
        digest = hashlib.sha256()
        with temp_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return rows, {
            "protocol": "sqlite_backup_v1",
            "source_path": str(path.resolve()),
            "snapshot_sha256": digest.hexdigest(),
            "snapshot_bytes": temp_path.stat().st_size,
            "wal_present": Path(str(path) + "-wal").is_file(),
            "shm_present": Path(str(path) + "-shm").is_file(),
        }
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _state_snapshot() -> dict[str, Any]:
    sqlite_paths = {
        path.resolve(strict=False)
        for path in (
            config.RAG_STORAGE_DIR / "doc_metadata_index.sqlite",
            config.CARD_FTS_DB_PATH,
            config.SOURCE_REGISTRY_DB_PATH,
            config.WIKI_STATE_DB_PATH,
        )
    }
    roots = [config.RAG_STORAGE_DIR, config.CARD_FTS_DB_PATH, config.SOURCE_REGISTRY_DB_PATH]
    if config.WIKI_ENABLED:
        roots.append(config.WIKI_STATE_DB_PATH)
    records: list[dict[str, Any]] = []
    for root in roots:
        candidates = [root] if root.is_file() else list(root.rglob("*")) if root.is_dir() else []
        for path in candidates:
            resolved = path.resolve(strict=False)
            # SQLite bytes are represented by the backup identities below.
            # Hashing the live main file separately from rows can mix a WAL
            # checkpoint with a different read view.
            if (
                path.is_file()
                and not path.is_symlink()
                and resolved not in sqlite_paths
                and path.suffix.casefold() not in {".sqlite", ".db"}
                and not path.name.casefold().endswith(("-wal", "-shm", "-journal"))
            ):
                records.append({
                    "path": str(resolved.relative_to(config.PROJECT_ROOT.resolve())).replace("\\", "/"),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                })
    records.sort(key=lambda item: item["path"].casefold())
    metadata_path = config.RAG_STORAGE_DIR / "doc_metadata_index.sqlite"
    metadata_snapshot, metadata_identity = _stable_sqlite_snapshot(
        metadata_path, {"aliases": "SELECT source_path, metadata_json FROM source_metadata ORDER BY source_path"}
    )
    fts_snapshot, fts_identity = _stable_sqlite_snapshot(
        config.CARD_FTS_DB_PATH,
        {
            "cards": "SELECT source_id, card_path, normalized_file, post_url, title, search_text, entities, topics, claim_types FROM cards_fts ORDER BY source_id, card_path",
            "youtube": "SELECT segment_id, parent_source_id, video_id, segment_index, start_seconds, end_seconds, start_url, card_path, title, search_text FROM youtube_segments_fts ORDER BY segment_id",
        },
    )
    registry_snapshot, registry_identity = _stable_sqlite_snapshot(
        config.SOURCE_REGISTRY_DB_PATH,
        {
            "sources": "SELECT source_id, normalized_file, card_path, post_url FROM sources ORDER BY source_id",
            "normalized_docs": "SELECT source_id, normalized_file FROM normalized_docs ORDER BY source_id",
            "enriched_cards": "SELECT source_id, card_path, content_hash FROM enriched_cards ORDER BY source_id",
            "references": "SELECT source_id, reference_type, url, label FROM \"references\" ORDER BY id",
        },
    )
    wiki_rows: list[list[Any]] = []
    wiki_documents: list[list[Any]] = []
    wiki_identity: dict[str, Any] | None = None
    if config.WIKI_ENABLED:
        wiki_snapshot, wiki_identity = _stable_sqlite_snapshot(
            config.WIKI_STATE_DB_PATH,
            {
                "wiki": "SELECT rowid, title, body FROM wiki_fts ORDER BY rowid",
                "documents": "SELECT rowid, document_key, document_kind, scope_key, title, body, source_ref_json, projection_output_hash, updated_at FROM wiki_fts_documents ORDER BY rowid",
            },
        )
        wiki_rows = wiki_snapshot["wiki"]
        wiki_documents = wiki_snapshot["documents"]
    payload = {
        "schema_version": "graph_state_snapshot_v1",
        "files": records,
        "metadata_aliases": metadata_snapshot["aliases"],
        "fts": {
            "cards": fts_snapshot["cards"],
            "youtube": fts_snapshot["youtube"],
            "wiki": wiki_rows,
            "wiki_documents": wiki_documents,
        },
        "registry": registry_snapshot,
        "sqlite_snapshots": {
            "metadata": metadata_identity,
            "fts": fts_identity,
            "registry": registry_identity,
            "wiki": wiki_identity,
        },
    }
    payload["snapshot_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    return payload


def reconcile() -> dict:
    status_path = config.RAG_STORAGE_DIR / "kv_store_doc_status.json"
    payload = strict_json_loads(status_path.read_text(encoding="utf-8"))
    metadata_index = config.RAG_STORAGE_DIR / "doc_metadata_index.sqlite"
    items = []
    source_ids: dict[str, str] = {}
    for doc_id, record in sorted(payload.items()):
        file_path = str(record.get("file_path") or "") if isinstance(record, dict) else ""
        passport = resolve_source_path(
            file_path,
            config.SOURCE_REGISTRY_DB_PATH,
            metadata_index,
        )
        status = str(record.get("status") or "") if isinstance(record, dict) else ""
        classification = "matched"
        source_id = ""
        if passport is None:
            classification = "missing"
        else:
            source_id = passport.source_id
            if source_id in source_ids:
                classification = "ambiguous"
            else:
                source_ids[source_id] = doc_id
        if status.casefold().rsplit(".", 1)[-1] != "processed":
            classification = "nonterminal"
        items.append(
            {
                "doc_id": doc_id,
                "file_path": file_path,
                "source_id": source_id,
                "classification": classification,
            }
        )
    counts = {
        key: sum(item["classification"] == key for item in items)
        for key in ("matched", "missing", "ambiguous", "nonterminal")
    }
    report = {
        "schema_version": "graph_provenance_reconciliation_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "records": len(items),
        "unique_source_ids": len(source_ids),
        "source_ids": sorted(source_ids),
        "doc_ids": sorted(source_ids.values()),
        "counts": counts,
        "passed": counts == {"matched": len(items), "missing": 0, "ambiguous": 0, "nonterminal": 0},
        "items": items,
        "state_snapshot": _state_snapshot(),
    }
    return report


def main() -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, help="accepted_manifest_v2 used for release identity")
    parser.add_argument("--release-run-id", help="release run id; required with --manifest")
    args = parser.parse_args()
    safe_resolve_release_path(args.report, label="provenance_report_path")
    preflight = None
    if args.manifest is not None:
        if not args.release_run_id:
            parser.error("--release-run-id is required with --manifest")
        preflight = preflight_release_identity(args.release_run_id, args.manifest)
    elif args.release_run_id:
        parser.error("--manifest is required with --release-run-id")
    report = reconcile()
    if preflight is not None:
        identity = preflight.identity
        report = stamp_report(
            report,
            identity,
            "python scripts/graph_provenance_reconcile.py " + " ".join(sys.argv[1:]),
            producer="scripts.graph_provenance_reconcile",
            producer_schema_version="graph_provenance_reconciliation_v1",
            source_inputs={
                "manifest_sha256": identity["manifest_sha256"],
                "corpus_identity_sha256": identity["corpus_identity_sha256"],
            },
            command_argv=["python", "scripts/graph_provenance_reconcile.py", *sys.argv[1:]],
        )
    report_path = args.report.resolve()
    atomic_write_release_json(report_path, report, label="provenance_report")
    print(json.dumps({key: report[key] for key in ("records", "unique_source_ids", "counts", "passed")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(run_release_cli(main, component="graph_provenance_reconcile"))
