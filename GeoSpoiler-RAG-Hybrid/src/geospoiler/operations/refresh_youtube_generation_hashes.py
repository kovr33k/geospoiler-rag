"""Refresh byte hashes in already-published YouTube generation manifests.

Path portability migration may reserialize JSON without changing semantic
content.  This command updates only the manifest's byte hashes, after parsing
every card and segment and preserving the generation fingerprint/status.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from geospoiler.contracts.models import EnrichedCardV2, YouTubeSegmentCardV2
from geospoiler.ingestion.enricher.youtube_pipeline import (
    _episode_card_path,
    _iter_dedicated_sources,
    _segment_dir,
    _source_fingerprint,
)
from geospoiler.ingestion.enricher.youtube_segmenter import build_segment_specs, needs_youtube_segments


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def refresh() -> dict[str, int]:
    updated = 0
    checked = 0
    for source in _iter_dedicated_sources(None, []):
        card_path = _episode_card_path(source)
        manifest_path = card_path.with_name(f"{card_path.name}.manifest.json")
        if not card_path.is_file() or not manifest_path.is_file():
            raise FileNotFoundError(f"missing YouTube generation: {card_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("status") != "complete":
            raise ValueError(f"manifest is not complete: {manifest_path}")
        transcript = str(source.get("transcript_text") or "").strip()
        specs = (
            build_segment_specs(transcript, cues=source.get("cues") or [], chapters=source.get("chapters") or [])
            if needs_youtube_segments(transcript, source.get("duration_seconds"))
            else []
        )
        expected_fingerprint = _source_fingerprint(source, transcript)
        if manifest.get("fingerprint") != expected_fingerprint:
            raise ValueError(f"fingerprint mismatch: {manifest_path}")
        expected_names = [f"{spec.index:04d}.youtube-segment.json" for spec in specs]
        segment_dir = _segment_dir(source)
        actual_names = sorted(path.name for path in segment_dir.glob("*.youtube-segment.json")) if segment_dir.is_dir() else []
        if actual_names != expected_names or manifest.get("segment_files") != expected_names:
            raise ValueError(f"segment set mismatch: {manifest_path}")
        # Parsing is intentional: hash refresh must never bless malformed JSON.
        EnrichedCardV2.model_validate_json(card_path.read_text(encoding="utf-8"))
        segment_hashes: dict[str, str] = {}
        for name in expected_names:
            segment_path = segment_dir / name
            YouTubeSegmentCardV2.model_validate_json(segment_path.read_text(encoding="utf-8"))
            segment_hashes[name] = _sha256(segment_path)
        new_card_hash = _sha256(card_path)
        changed = manifest.get("card_sha256") != new_card_hash or manifest.get("segment_sha256") != segment_hashes
        if changed:
            manifest["card_sha256"] = new_card_hash
            manifest["segment_sha256"] = segment_hashes
            serialized = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            fd, temp_name = tempfile.mkstemp(prefix=f".{manifest_path.name}.", suffix=".tmp", dir=manifest_path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, manifest_path)
            finally:
                Path(temp_name).unlink(missing_ok=True)
            updated += 1
        checked += 1
    return {"checked": checked, "updated": updated}


if __name__ == "__main__":
    print(json.dumps(refresh(), ensure_ascii=False, sort_keys=True))
