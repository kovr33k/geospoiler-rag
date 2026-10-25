"""Prepare, activate, and if necessary restore a full Luna re-enrichment run."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geospoiler import config  # noqa: E402
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.contracts.models import NormalizedMeta  # noqa: E402
from geospoiler.ingestion.enricher.pipeline import _is_youtube_only_normalized_document  # noqa: E402
from geospoiler.operations.release_identity import (
    SafeArgumentParser,
    atomic_write_release_json,
    run_release_cli,
    safe_exception_token,
    safe_resolve_release_path,
)
from geospoiler.providers import llm_backend  # noqa: E402

RUNS_ROOT = PROJECT_ROOT / "artifacts" / "luna_full_reenrich"
EXPECTED_MODEL = "codex-cli:gpt-5.6-luna@xhigh"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()


def _write_json(path: Path, value: Any) -> None:
    label = "luna_reenrich_control"
    if isinstance(value, dict) and value.get("schema_version"):
        label = str(value["schema_version"])
    atomic_write_release_json(path, value, label=label)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _file_records(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    return [
        {"path": _relative(path), "size": path.stat().st_size, "sha256": _sha256(path)}
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    ]


def _input_identity() -> dict[str, Any]:
    normalized_files = _file_records(config.NORMALIZED_DIR)
    youtube_files = _file_records(config.YOUTUBE_NORMALIZED_DIR)
    generic_jobs = 0
    youtube_only = 0
    source_ids: set[str] = set()
    for text_path in sorted(config.NORMALIZED_DIR.rglob("*.txt")):
        meta_path = text_path.with_suffix(".meta.json")
        meta = NormalizedMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
        source_ids.add(meta.source_id.value)
        if _is_youtube_only_normalized_document(meta, text_path):
            youtube_only += 1
        else:
            generic_jobs += 1

    video_ids: set[str] = set()
    for metadata_path in sorted(config.YOUTUBE_NORMALIZED_DIR.rglob("*.youtube.meta.json")):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        video_ids.add(str(metadata["video_id"]))

    return {
        "files": normalized_files + youtube_files,
        "counts": {
            "normalized_documents": generic_jobs + youtube_only,
            "generic_jobs": generic_jobs,
            "youtube_only_documents": youtube_only,
            "youtube_sources": len(video_ids),
            "unique_source_ids": len(source_ids),
            "unique_video_ids": len(video_ids),
        },
    }


def _runtime_identity() -> dict[str, Any]:
    return {
        "LLM_PROFILE": config.LLM_PROFILE,
        "CODEX_LUNA_MODEL": config.CODEX_LUNA_MODEL,
        "CODEX_LUNA_REASONING_EFFORT": config.CODEX_LUNA_REASONING_EFFORT,
        "CODEX_FALLBACK_TO_API": config.CODEX_FALLBACK_TO_API,
        "ENRICHMENT_SCHEMA_VERSION": config.ENRICHMENT_SCHEMA_VERSION,
        "ENRICHMENT_PROMPT_VERSION": config.ENRICHMENT_PROMPT_VERSION,
        "YOUTUBE_ENRICHMENT_PROMPT_VERSION": config.YOUTUBE_ENRICHMENT_PROMPT_VERSION,
        "active_enrichment_model": llm_backend.active_model_for("enrichment"),
        "WIKI_ENABLED": config.WIKI_ENABLED,
        "RERANKER_ENABLED": config.RERANKER_ENABLED,
    }


def _old_corpus_identity() -> dict[str, Any]:
    models: Counter[str] = Counter()
    cards = 0
    for path in sorted(config.ENRICHED_DIR.rglob("*.enriched.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        models[str(data.get("enrichment_model") or "<missing>")] += 1
        cards += 1
    return {"cards": cards, "model_distribution": dict(sorted(models.items()))}


def _assert_preflight(identity: dict[str, Any]) -> None:
    counts = identity["input"]["counts"]
    normalized_documents = int(counts.get("normalized_documents") or 0)
    generic_jobs = int(counts.get("generic_jobs") or 0)
    youtube_only = int(counts.get("youtube_only_documents") or 0)
    unique_source_ids = int(counts.get("unique_source_ids") or 0)

    # Corpus size is a live input invariant, not a fixture. Deleted Telegram
    # posts must not make a valid backup impossible; duplicates and accounting
    # drift must still fail closed.
    if normalized_documents != generic_jobs + youtube_only:
        raise RuntimeError(f"Input accounting mismatch: {counts}")
    if unique_source_ids != normalized_documents:
        raise RuntimeError(f"Input source identity mismatch: {counts}")
    if counts.get("youtube_sources") != 4 or counts.get("unique_video_ids") != 4:
        raise RuntimeError(f"YouTube input identity mismatch: {counts}")

    runtime = identity["runtime"]
    if runtime["active_enrichment_model"] != EXPECTED_MODEL:
        raise RuntimeError(f"Unexpected active model: {runtime['active_enrichment_model']}")
    if runtime["CODEX_FALLBACK_TO_API"]:
        raise RuntimeError("CODEX_FALLBACK_TO_API must be false")
    if runtime["WIKI_ENABLED"] or runtime["RERANKER_ENABLED"]:
        raise RuntimeError("Wiki and reranker must be disabled during re-enrichment")


def _backup_sources() -> list[Path]:
    sources = [
        config.NORMALIZED_DIR,
        config.TRANSCRIPTION_DIR,
        config.MEDIA_VISUAL_DIR,
        config.ENRICHED_DIR,
        config.YOUTUBE_SEGMENTS_DIR,
        config.RAG_STORAGE_DIR,
        config.STATE_DIR / "enrichment_progress.json",
        config.YOUTUBE_CHECKPOINT_DIR,
    ]
    for database in (config.CARD_FTS_DB_PATH, config.SOURCE_REGISTRY_DB_PATH):
        sources.extend(sorted(database.parent.glob(f"{database.name}*")))
    unique: dict[str, Path] = {}
    for path in sources:
        if path.exists():
            unique[str(path.resolve()).casefold()] = path
    return list(unique.values())


def _backup_bytes() -> int:
    total = 0
    for source in _backup_sources():
        if source.is_dir():
            total += sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
        else:
            total += source.stat().st_size
    return total


def prepare(run_dir: Path, *, confirm_live_smoke: bool = False) -> None:
    run_dir = run_dir.resolve()
    run_dir.relative_to(RUNS_ROOT.resolve())
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    usage = shutil.disk_usage(PROJECT_ROOT)
    backup_bytes = _backup_bytes()
    if usage.free < backup_bytes * 4:
        raise RuntimeError(
            f"Insufficient free space: {usage.free} bytes available for {backup_bytes} backup bytes"
        )
    smoke = {"status": "not_run", "passed": False, "real_calls": 0}
    if confirm_live_smoke:
        smoke_result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "luna_smoke.py"), "--confirm-live"],
            cwd=PROJECT_ROOT,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        smoke = {
            "status": "passed" if smoke_result.returncode == 0 else "failed",
            "passed": smoke_result.returncode == 0,
            "real_calls": 2 if smoke_result.returncode == 0 else 0,
            "exit_code": smoke_result.returncode,
        }
        if smoke_result.returncode != 0:
            raise RuntimeError("Live Luna smoke failed; backup preparation refused")

    identity = {
        "run_id": run_dir.name,
        "created_at": datetime.now(UTC).isoformat(),
        "git": {
            "branch": _git("branch", "--show-current"),
            "commit": _git("rev-parse", "HEAD"),
            "status": _git("status", "--short"),
        },
        "runtime": _runtime_identity(),
        "input": _input_identity(),
        "old_corpus": _old_corpus_identity(),
        "disk": {
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "backup_bytes": backup_bytes,
            "minimum_required_bytes": backup_bytes * 4,
        },
        "luna_smoke": smoke,
    }
    _assert_preflight(identity)
    _write_json(run_dir / "preflight.json", identity)

    backup_root = run_dir / "backup"
    copied: list[dict[str, Any]] = []
    for source in _backup_sources():
        destination = backup_root / _relative(source)
        if source.is_dir():
            shutil.copytree(source, destination)
            files = sorted(item for item in source.rglob("*") if item.is_file())
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            files = [source]
        for original in files:
            backup = backup_root / _relative(original)
            original_hash = _sha256(original)
            backup_hash = _sha256(backup)
            if original_hash != backup_hash:
                raise RuntimeError(f"Backup hash mismatch: {original}")
            copied.append(
                {
                    "source": _relative(original),
                    "path": backup.relative_to(backup_root).as_posix(),
                    "bytes": original.stat().st_size,
                    "sha256": original_hash,
                }
            )
    _write_json(
        run_dir / "backup_manifest.json",
        {
            "schema_version": "backup_source_manifest_v1",
            "created_at": datetime.now(UTC).isoformat(),
            "root": str(backup_root),
            "mapping_version": "canonical_v1",
            "files": copied,
            "verified": True,
        },
    )
    print(json.dumps({"run_id": run_dir.name, "backup_files": len(copied), **identity["input"]["counts"]}))


def _validated_backup_manifest(run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / "backup_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") not in {
        "backup_source_manifest_v1",
        "backup_restore_closeout_v1",  # read-only compatibility for pre-R17 runs
    }:
        raise RuntimeError("Unsupported backup manifest schema")
    records = payload.get("files")
    if not payload.get("verified") or not isinstance(records, list) or not records:
        raise RuntimeError("Verified backup manifest is required")
    root_value = payload.get("root")
    backup_root = Path(str(root_value or "")).resolve()
    expected_root = (run_dir / "backup").resolve()
    if backup_root != expected_root:
        raise RuntimeError(f"Unexpected backup root: {backup_root}")
    seen: set[str] = set()
    seen_sources: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise RuntimeError("Invalid backup manifest record")
        relative = Path(str(record.get("path") or ""))
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise RuntimeError(f"Unsafe backup path: {relative}")
        backup = (backup_root / relative).resolve()
        try:
            backup.relative_to(backup_root)
        except ValueError:
            raise RuntimeError(f"Backup path escapes run directory: {relative}") from None
        key = str(backup).casefold()
        if key in seen:
            raise RuntimeError(f"Duplicate backup path: {relative}")
        seen.add(key)
        if not backup.is_file():
            raise RuntimeError(f"Missing backup file: {relative}")
        if backup.stat().st_size != record.get("bytes") or _sha256(backup) != record.get("sha256"):
            raise RuntimeError(f"Backup hash mismatch: {relative}")
        source_relative = Path(str(record.get("source") or ""))
        if source_relative.is_absolute() or any(
            part in {"", ".", ".."} for part in source_relative.parts
        ):
            raise RuntimeError(f"Unsafe backup source: {source_relative}")
        source = (PROJECT_ROOT / source_relative).resolve()
        try:
            source.relative_to(PROJECT_ROOT.resolve())
        except ValueError:
            raise RuntimeError(f"Backup source escapes project: {source_relative}") from None
        source_key = str(source).casefold()
        if source_key in seen_sources:
            raise RuntimeError(f"Duplicate backup source: {source_relative}")
        seen_sources.add(source_key)
        if backup.suffix.casefold() in {".sqlite", ".db"}:
            import sqlite3
            with sqlite3.connect(f"{backup.as_uri()}?mode=ro", uri=True) as connection:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError(f"SQLite integrity check failed: {relative}")
    return payload


def activate(run_dir: Path) -> None:
    run_dir = run_dir.resolve()
    run_dir.relative_to(RUNS_ROOT.resolve())
    manifest = _validated_backup_manifest(run_dir)
    for record in manifest["files"]:
        source = PROJECT_ROOT / record["source"]
        backup = Path(manifest["root"]) / record["path"]
        if (
            not source.is_file()
            or _sha256(source) != record["sha256"]
            or not backup.is_file()
            or _sha256(backup) != record["sha256"]
        ):
            raise RuntimeError(f"Source or backup changed after preparation: {record['source']}")

    retired_root = run_dir / "retired"
    targets = [
        config.ENRICHED_DIR,
        config.YOUTUBE_SEGMENTS_DIR,
        config.STATE_DIR / "enrichment_progress.json",
        config.YOUTUBE_CHECKPOINT_DIR,
    ]
    moved: list[dict[str, str]] = []
    for target in targets:
        target = target.resolve()
        target.relative_to(PROJECT_ROOT.resolve())
        if not target.exists():
            continue
        destination = retired_root / _relative(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"Retirement target exists: {destination}")
        shutil.move(str(target), str(destination))
        moved.append({"from": _relative(target), "to": destination.relative_to(run_dir).as_posix()})

    config.ENRICHED_DIR.mkdir(parents=True, exist_ok=True)
    config.YOUTUBE_SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)
    config.YOUTUBE_CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(
        run_dir / "maintenance.json",
        {
            "activated_at": datetime.now(UTC).isoformat(),
            "moved": moved,
            "indexes_left_active": [
                _relative(config.CARD_FTS_DB_PATH),
                _relative(config.SOURCE_REGISTRY_DB_PATH),
            ],
            "rollback_command": (
                f'python scripts/luna_reenrich_control.py restore --run-dir "{run_dir}" '
                "--confirm-restore"
            ),
        },
    )
    print(json.dumps({"activated": True, "run_id": run_dir.name, "moved": len(moved)}))


_RESTORE_TARGET_PHASES = {"planned", "moving", "moved", "copying", "copied", "verified", "rolled_back"}
_RESTORE_TERMINAL_PHASES = {"committed", "rolled_back"}


def _journal_path(value: Any, *, root: Path, project: Path | None = None) -> Path:
    candidate = Path(str(value or ""))
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    boundary = (project or root).resolve()
    try:
        resolved.relative_to(boundary)
    except ValueError:
        raise RuntimeError("Restore journal path escapes boundary") from None
    return resolved


def _verify_journal_records(root: Path, records: list[dict[str, Any]], *, relative_base: str = "") -> None:
    base = Path(relative_base) if relative_base else Path()
    for record in records:
        source = Path(str(record.get("source") or ""))
        if source.is_absolute() or any(part in {"", ".", ".."} for part in source.parts):
            raise RuntimeError(f"Unsafe restore journal source: {source}")
        try:
            suffix = source.relative_to(base)
        except ValueError:
            raise RuntimeError(f"Journal source is outside target: {source}") from None
        path = (root / suffix).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            raise RuntimeError(f"Journal record escapes retired root: {source}") from None
        if not path.is_file() or _sha256(path) != record.get("sha256"):
            raise RuntimeError(f"Restore journal SHA mismatch: {source}")


def _recover_restore_journal(journal_path: Path, run_dir: Path) -> None:
    """Rollback an interrupted restore before another restore can start."""
    if not journal_path.is_file():
        return
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if not isinstance(journal, dict):
            raise ValueError("journal root must be an object")
        if journal.get("schema_version") != "restore_journal_v2":
            raise ValueError("unsupported restore journal schema")
        if Path(str(journal.get("run_dir") or "")).resolve() != run_dir.resolve():
            raise ValueError("restore journal run directory mismatch")
        phase = str(journal.get("phase") or "")
        if phase in _RESTORE_TERMINAL_PHASES:
            return
        if phase not in {"prepared", "moving", "copying", "verifying"}:
            raise ValueError(f"unsupported restore journal phase: {phase!r}")
        records = journal.get("targets")
        if not isinstance(records, list) or not records:
            raise ValueError("restore journal targets are missing")
        validated: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("invalid restore journal target record")
            target = _journal_path(record.get("target"), root=run_dir, project=PROJECT_ROOT)
            retired = _journal_path(record.get("retired"), root=run_dir)
            backup = _journal_path(record.get("backup"), root=run_dir)
            target_phase = str(record.get("phase") or "")
            if target_phase not in _RESTORE_TARGET_PHASES:
                raise ValueError(f"unsupported target phase: {target_phase!r}")
            matching = record.get("matching")
            if not isinstance(matching, list):
                raise ValueError("restore journal matching records are missing")
            if not backup.exists():
                raise ValueError(f"restore backup is missing: {backup}")
            initial_exists = bool(record.get("initial_exists"))
            if target_phase == "moving":
                # A crash may happen after the journal says "moving" but
                # before or after shutil.move.  Resolve that ambiguity from
                # the filesystem instead of assuming either side won.
                if initial_exists and retired.exists():
                    target_phase = "moved"
                elif initial_exists and target.exists():
                    target_phase = "planned"
                elif not initial_exists and not target.exists():
                    target_phase = "planned"
                else:
                    raise ValueError(
                        "ambiguous restore moving phase: target/retired state cannot be resolved"
                    )
            if target_phase in {"moved", "copying", "copied", "verified"} and initial_exists:
                if not retired.exists():
                    raise ValueError(f"retired target is missing: {retired}")
                _verify_journal_records(
                    retired,
                    matching,
                    relative_base=str(record.get("relative") or ""),
                )
            validated.append(
                {
                    "target": target,
                    "retired": retired,
                    "phase": target_phase,
                    "initial_exists": initial_exists,
                }
            )
        journal["phase"] = "recovering"
        _write_json(journal_path, journal)
        for item in reversed(validated):
            target = item["target"]
            retired = item["retired"]
            if item["phase"] not in {"moved", "moving", "copying", "copied", "verified"}:
                continue
            if item["initial_exists"]:
                if target.exists() or target.is_symlink():
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                if retired.exists():
                    shutil.move(str(retired), str(target))
                elif not target.exists():
                    raise RuntimeError(f"cannot recover missing retired target: {retired}")
            elif target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
        journal["phase"] = "rolled_back"
        journal["recovered_from_phase"] = phase
        for record in journal["targets"]:
            record["phase"] = "rolled_back"
        _write_json(journal_path, journal)
        return
    except Exception as exc:
        token = safe_exception_token(
            "restore_journal_recovery_failed",
            exc,
            component="luna_reenrich_control",
        )
    raise RuntimeError(f"Restore journal recovery failed:{token}") from None


def restore(run_dir: Path, confirm: bool) -> None:
    if not confirm:
        raise RuntimeError("Restore requires --confirm-restore")
    run_dir = run_dir.resolve()
    run_dir.relative_to(RUNS_ROOT.resolve())
    journal_path = run_dir / "restore_journal.json"
    _recover_restore_journal(journal_path, run_dir)
    manifest = _validated_backup_manifest(run_dir)
    backup_root = run_dir / "backup"
    source_records = {str(record.get("source") or ""): record for record in manifest["files"]}
    failed_root = run_dir / f"failed_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
    targets = [
        config.NORMALIZED_DIR,
        config.TRANSCRIPTION_DIR,
        config.MEDIA_VISUAL_DIR,
        config.ENRICHED_DIR,
        config.YOUTUBE_SEGMENTS_DIR,
        config.RAG_STORAGE_DIR,
        config.STATE_DIR / "enrichment_progress.json",
        config.YOUTUBE_CHECKPOINT_DIR,
    ]
    database_names = (config.CARD_FTS_DB_PATH.name, config.SOURCE_REGISTRY_DB_PATH.name)
    database_paths: dict[str, Path] = {}
    for database in (config.CARD_FTS_DB_PATH, config.SOURCE_REGISTRY_DB_PATH):
        for current in database.parent.glob(f"{database.name}*"):
            database_paths[str(current.resolve()).casefold()] = current
    backup_artifacts = backup_root / "artifacts"
    if backup_artifacts.exists():
        for backup in backup_artifacts.iterdir():
            if backup.is_file() and backup.name.startswith(database_names):
                current = PROJECT_ROOT / backup.relative_to(backup_root)
                database_paths[str(current.resolve()).casefold()] = current
    targets.extend(database_paths.values())

    def remove_target(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()

    plans: list[dict[str, Any]] = []
    for raw_target in targets:
        target = raw_target.resolve()
        target.relative_to(PROJECT_ROOT.resolve())
        relative_target = _relative(target)
        matching = [
            record
            for source, record in source_records.items()
            if source == relative_target
            or source.startswith(relative_target.rstrip("/") + "/")
        ]
        if target.suffix and not matching:
            raise RuntimeError(f"No verified backup record for restore target: {relative_target}")
        backup = (backup_root / relative_target).resolve()
        backup.relative_to(backup_root.resolve())
        if target.is_dir() and not backup.is_dir():
            raise RuntimeError(f"Missing directory backup for restore target: {relative_target}")
        if target.is_file() and not backup.is_file():
            raise RuntimeError(f"Missing file backup for restore target: {relative_target}")
        if not target.exists() and not backup.exists():
            raise RuntimeError(f"Missing restore source: {relative_target}")
        plans.append(
            {
                "target": target,
                "relative": relative_target,
                "backup": backup,
                "matching": matching,
                "initial_exists": target.exists(),
            }
        )

    journal: dict[str, Any] = {
        "schema_version": "restore_journal_v2",
        "phase": "prepared",
        "run_dir": str(run_dir),
        "failed_root": str(failed_root),
        "targets": [
            {
                "relative": plan["relative"],
                "target": str(plan["target"]),
                "retired": str(failed_root / plan["relative"]),
                "backup": str(plan["backup"]),
                "initial_exists": plan["initial_exists"],
                "phase": "planned",
                "matching": plan["matching"],
            }
            for plan in plans
        ],
        "completed": [],
    }
    _write_json(journal_path, journal)
    moved: list[tuple[Path, Path]] = []
    created: list[Path] = []
    try:
        for index, plan in enumerate(plans):
            target: Path = plan["target"]
            relative_target = plan["relative"]
            backup: Path = plan["backup"]
            journal["phase"] = "moving"
            journal["targets"][index]["phase"] = "moving"
            _write_json(journal_path, journal)
            if target.exists():
                destination = failed_root / relative_target
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(destination))
                moved.append((target, destination))
            else:
                # Track targets before copying.  copytree/copy2 can leave a
                # partially-created target when the process is interrupted or
                # the destination filesystem rejects a later write; rollback
                # must remove that target even though it did not exist at the
                # beginning of restore.
                created.append(target)
            journal["targets"][index]["phase"] = "moved"
            _write_json(journal_path, journal)
            journal["phase"] = "copying"
            journal["targets"][index]["phase"] = "copying"
            _write_json(journal_path, journal)
            if backup.is_dir():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(backup, target)
            elif backup.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
            else:
                raise RuntimeError(f"Restore backup disappeared: {relative_target}")
            journal["targets"][index]["phase"] = "copied"
            journal["completed"].append(relative_target)
            _write_json(journal_path, journal)

        journal["phase"] = "verifying"
        _write_json(journal_path, journal)
        for index, plan in enumerate(plans):
            for record in plan["matching"]:
                restored = (PROJECT_ROOT / Path(record["source"])).resolve()
                if not restored.is_file() or _sha256(restored) != record["sha256"]:
                    raise RuntimeError(f"Post-restore hash mismatch: {record['source']}")
            journal["targets"][index]["phase"] = "verified"
            _write_json(journal_path, journal)
        journal["phase"] = "committed"
        _write_json(journal_path, journal)
    except Exception:
        for target in reversed(created):
            try:
                remove_target(target)
            except Exception as rollback_exc:
                journal.setdefault("rollback_errors", []).append(
                    safe_exception_token(
                        "restore_rollback_failed",
                        rollback_exc,
                        component="luna_reenrich_control",
                    )
                )
        for target, retired in reversed(moved):
            try:
                remove_target(target)
                if retired.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(retired), str(target))
            except Exception as rollback_exc:
                journal.setdefault("rollback_errors", []).append(
                    safe_exception_token(
                        "restore_rollback_failed",
                        rollback_exc,
                        component="luna_reenrich_control",
                    )
                )
        journal["phase"] = "rolled_back"
        for record in journal.get("targets", []):
            if isinstance(record, dict):
                record["phase"] = "rolled_back"
        _write_json(journal_path, journal)
        raise
    print(json.dumps({"restored": True, "run_id": run_dir.name, "targets": len(plans)}))


def main() -> int:
    parser = SafeArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "activate", "restore"):
        command = subparsers.add_parser(name)
        command.add_argument("--run-dir", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--confirm-live-smoke", action="store_true")
        if name == "restore":
            command.add_argument("--confirm-restore", action="store_true")
    args = parser.parse_args()
    run_dir = safe_resolve_release_path(
        args.run_dir, label="luna_reenrich_run_dir"
    )
    if args.command == "prepare":
        prepare(run_dir, confirm_live_smoke=args.confirm_live_smoke)
    elif args.command == "activate":
        activate(run_dir)
    else:
        restore(run_dir, args.confirm_restore)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_release_cli(main, component="luna_reenrich_control"))
