"""Unified operator-facing native media backfill orchestration."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geospoiler.ingestion.normalizer.transcription_backfill import backfill_transcripts
from geospoiler.ingestion.normalizer.visual_backfill import backfill_visuals


def run_media_backfill(
    *,
    limit: int | None = None,
    channel: str | None = None,
    message_ids: set[int] | None = None,
    dry_run: bool = True,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Run both media routes and emit a machine-readable report."""
    transcript = backfill_transcripts(
        limit=limit,
        channel=channel,
        message_ids=message_ids,
        dry_run=dry_run,
    )
    visual = backfill_visuals(
        limit=limit,
        channel=channel,
        message_ids=message_ids,
        dry_run=dry_run,
    )
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "dry_run": dry_run,
        "selection": {
            "limit": limit,
            "channel": channel,
            "message_ids": sorted(message_ids or set()),
        },
        "transcription": asdict(transcript),
        "visual": asdict(visual),
    }
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = report_path.with_name(f".{report_path.name}.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(report_path)
        report["report_path"] = str(report_path)
    return report
