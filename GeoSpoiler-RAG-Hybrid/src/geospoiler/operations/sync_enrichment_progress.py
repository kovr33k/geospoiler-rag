"""Synchronize deterministic enrichment progress fingerprints after path migration.

This command does not call an LLM.  It only recomputes the source fingerprint
from the current normalized text/meta bytes and records the active media
contract version for existing generic enrichment entries.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from geospoiler import config
from geospoiler.ingestion.enricher.pipeline import _source_fingerprint


def sync_progress(path: Path | None = None) -> dict[str, int]:
    progress_path = path or (config.STATE_DIR / "enrichment_progress.json")
    payload = json.loads(progress_path.read_text(encoding="utf-8"))
    entries = payload.get("enriched")
    if not isinstance(entries, dict):
        raise ValueError("enrichment_progress.json.enriched must be an object")

    updated = 0
    missing = 0
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            raise ValueError(f"progress entry {key!r} must be an object")
        text_path = config.NORMALIZED_DIR / Path(f"{key}.txt")
        meta_path = text_path.with_suffix(".meta.json")
        if not text_path.is_file() or not meta_path.is_file():
            missing += 1
            continue
        fingerprint = _source_fingerprint(text_path, meta_path)
        if entry.get("source_fingerprint") != fingerprint or entry.get("media_contract_version") != "media-contract-v2":
            entry["source_fingerprint"] = fingerprint
            entry["media_contract_version"] = "media-contract-v2"
            updated += 1

    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{progress_path.name}.", suffix=".tmp", dir=progress_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, progress_path)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return {"entries": len(entries), "updated": updated, "missing": missing}


if __name__ == "__main__":
    print(json.dumps(sync_progress(), ensure_ascii=False, sort_keys=True))
