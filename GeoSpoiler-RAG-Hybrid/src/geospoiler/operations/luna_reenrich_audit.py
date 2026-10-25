"""Acceptance audit for the immutable Luna Enriched v2 regeneration run."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from geospoiler import config
from geospoiler.contracts.models import EnrichedCardV2, LLMPayload, NormalizedMeta, YouTubeSegmentCardV2
from geospoiler.ingestion.enricher.pipeline import _is_youtube_only_normalized_document, _source_fingerprint
from geospoiler.ingestion.enricher.validator import required_russian_violations
from geospoiler.ingestion.enricher.youtube_pipeline import (
    _episode_card_path,
    _iter_dedicated_sources,
    _segment_dir,
    _validate_generation,
)
from geospoiler.ingestion.enricher.youtube_pipeline import _source_fingerprint as youtube_source_fingerprint
from geospoiler.ingestion.enricher.youtube_segmenter import build_segment_specs, needs_youtube_segments
from geospoiler.operations.release_identity import (
    RELEASE_EVIDENCE_SCHEMA,
    SafeArgumentParser,
    assert_no_release_secrets,
    atomic_write_release_json,
    atomic_write_release_text,
    canonical_json,
    preflight_release_identity,
    run_release_cli,
    safe_exception_token,
    safe_resolve_release_path,
    stamp_report,
)

EXPECTED_MODEL = "codex-cli:gpt-5.6-luna@xhigh"
RUNS_ROOT = config.PROJECT_ROOT / "artifacts" / "luna_full_reenrich"
PAYLOAD_FIELDS = (
    "summary",
    "key_points",
    "entities",
    "topics",
    "theses",
    "quotes",
    "events",
    "search_phrases",
    "quality_flags",
)


def _valid_url(value: object) -> bool:
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _payload(card: EnrichedCardV2 | YouTubeSegmentCardV2) -> LLMPayload:
    data = card.model_dump(mode="json")
    return LLMPayload.model_validate({key: data.get(key) for key in PAYLOAD_FIELDS})


def _audit(run_id: str) -> dict:
    errors: list[str] = []
    counts: dict[str, int] = {}
    quality = Counter()

    def error(message: str) -> None:
        errors.append(message)

    normalized = sorted(config.NORMALIZED_DIR.rglob("*.txt"))
    generic: dict[str, Path] = {}
    normalized_ids: list[str] = []
    youtube_only = 0
    for text_path in normalized:
        meta_path = text_path.with_suffix(".meta.json")
        try:
            meta = NormalizedMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - production audit path
            error(
                f"normalized_invalid:{text_path}:"
                + safe_exception_token("normalized_invalid", exc, component="luna_reenrich_audit")
            )
            continue
        normalized_ids.append(meta.source_id.value)
        relative = text_path.relative_to(config.NORMALIZED_DIR)
        key = relative.with_suffix("").as_posix()
        card_path = config.ENRICHED_DIR / relative.with_suffix(".enriched.json")
        if _is_youtube_only_normalized_document(meta, text_path):
            youtube_only += 1
            if card_path.exists():
                error(f"youtube_only_has_generic_card:{key}")
            continue
        generic[key] = card_path
        if not card_path.is_file():
            error(f"generic_card_missing:{key}")
            continue
        try:
            card = EnrichedCardV2.model_validate_json(card_path.read_text(encoding="utf-8"))
            if card.provenance.source_id != meta.source_id.value:
                error(f"source_id_mismatch:{key}")
            if card.enrichment_model != EXPECTED_MODEL:
                error(f"model_mismatch:{card_path}")
            if required_russian_violations(_payload(card)):
                error(f"russian_violation:{card_path}")
            if not _valid_url(card.provenance.post_url):
                error(f"invalid_provenance_url:{card_path}")
        except Exception as exc:  # pragma: no cover - production audit path
            error(
                f"generic_card_invalid:{card_path}:"
                + safe_exception_token("generic_card_invalid", exc, component="luna_reenrich_audit")
            )

    counts.update(
        normalized_documents=len(normalized),
        generic_jobs=len(generic),
        youtube_only_documents=youtube_only,
        unique_normalized_source_ids=len(set(normalized_ids)),
    )
    if len(normalized_ids) != len(set(normalized_ids)):
        error("duplicate_normalized_source_ids")
    if counts["normalized_documents"] != counts["generic_jobs"] + youtube_only:
        error(f"input_accounting_mismatch:{counts}")
    if counts["unique_normalized_source_ids"] != counts["normalized_documents"]:
        error(f"input_source_identity_mismatch:{counts}")

    youtube_sources = list(_iter_dedicated_sources(None, []))
    expected_youtube_sources = len(youtube_sources)
    if expected_youtube_sources <= 0:
        error("youtube_source_count_unexpected:0")

    all_cards = sorted(config.ENRICHED_DIR.rglob("*.enriched.json"))
    generic_cards = [path for path in all_cards if ".youtube." not in path.name]
    episode_cards = [path for path in all_cards if ".youtube." in path.name]
    counts.update(
        enriched_cards=len(all_cards),
        generic_cards=len(generic_cards),
        youtube_episode_cards=len(episode_cards),
    )
    expected_cards = counts["generic_jobs"] + expected_youtube_sources
    if len(generic_cards) != counts["generic_jobs"] or len(episode_cards) != expected_youtube_sources or len(all_cards) != expected_cards:
        error(f"enriched_counts_unexpected:{counts}")

    source_ids: list[str] = []
    extraction_issues = 0
    for card_path in all_cards:
        try:
            card = EnrichedCardV2.model_validate_json(card_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - production audit path
            error(
                f"card_invalid:{card_path}:"
                + safe_exception_token("card_invalid", exc, component="luna_reenrich_audit")
            )
            continue
        source_ids.append(card.provenance.source_id)
        quality.update(card.quality_flags)
        extraction_issues += len(card.extraction_issues)
        if card.enrichment_model != EXPECTED_MODEL:
            error(f"model_mismatch:{card_path}")
        # The media contract belongs to the generic Telegram enrichment
        # pipeline.  Dedicated YouTube episode cards are produced by the
        # segment pipeline and intentionally do not carry this field.
        if ".youtube." not in card_path.name and card.media_contract_version != "media-contract-v2":
            error(f"media_contract_mismatch:{card_path}")
        if required_russian_violations(_payload(card)):
            error(f"russian_violation:{card_path}")
        if not _valid_url(card.provenance.post_url):
            error(f"invalid_provenance_url:{card_path}")
        for link in card.source_chain.external_links:
            if not _valid_url(link.get("url")):
                error(f"invalid_external_url:{card_path}")
    if len(source_ids) != len(set(source_ids)):
        error("duplicate_enriched_source_ids")
    counts.update(
        card_extraction_issues=extraction_issues,
        card_unstable_flags=sum(quality.get(flag, 0) for flag in ("extraction_unstable", "partial_segment_failure")),
    )

    progress_path = config.STATE_DIR / "enrichment_progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    entries = progress.get("enriched", {})
    counts["progress_entries"] = len(entries)
    if len(entries) != counts["generic_jobs"]:
        error("progress_entry_count_unexpected")
    for key in generic:
        entry = entries.get(key)
        text_path = config.NORMALIZED_DIR / Path(f"{key}.txt")
        meta_path = text_path.with_suffix(".meta.json")
        if not isinstance(entry, dict):
            error(f"progress_missing:{key}")
            continue
        if entry.get("source_fingerprint") != _source_fingerprint(text_path, meta_path):
            error(f"progress_fingerprint_mismatch:{key}")
        if entry.get("enrichment_model") != EXPECTED_MODEL:
            error(f"progress_model_mismatch:{key}")

    counts["youtube_sources"] = len(youtube_sources)
    expected_segments = 0
    segment_files = 0
    segment_unstable = 0
    segment_language = 0
    for source in youtube_sources:
        transcript = str(source.get("transcript_text") or "").strip()
        long_form = needs_youtube_segments(
            transcript, source.get("duration_seconds")
        ) if transcript else False
        specs = build_segment_specs(
            transcript,
            cues=source.get("cues") or [],
            chapters=source.get("chapters") or [],
        ) if long_form else []
        expected_segments += len(specs)
        card_path = _episode_card_path(source)
        if not card_path.is_file():
            error(f"youtube_episode_missing:{source.get('video_id')}")
            continue
        try:
            card = EnrichedCardV2.model_validate_json(card_path.read_text(encoding="utf-8"))
            if card.enrichment_model != EXPECTED_MODEL:
                error(f"youtube_model_mismatch:{card_path}")
            if {"extraction_unstable", "partial_segment_failure"} & set(card.quality_flags):
                error(f"youtube_episode_unstable:{card_path}")
            if card.extraction_issues:
                error(f"youtube_episode_issues:{card_path}")
            if required_russian_violations(_payload(card)):
                error(f"youtube_episode_russian_violation:{card_path}")
        except Exception as exc:  # pragma: no cover - production audit path
            error(
                f"youtube_episode_invalid:{card_path}:"
                + safe_exception_token("youtube_episode_invalid", exc, component="luna_reenrich_audit")
            )
        generation = _validate_generation(
            card_path,
            source,
            youtube_source_fingerprint(source, transcript),
            specs,
        )
        if not generation.valid:
            error(f"youtube_generation_invalid:{source.get('video_id')}:{';'.join(generation.violations)}")
        segment_paths = sorted(_segment_dir(source).glob("*.youtube-segment.json"))
        segment_files += len(segment_paths)
        for segment_path in segment_paths:
            try:
                segment = YouTubeSegmentCardV2.model_validate_json(segment_path.read_text(encoding="utf-8"))
                if segment.enrichment_model != EXPECTED_MODEL:
                    error(f"youtube_segment_model_mismatch:{segment_path}")
                if {"extraction_unstable", "partial_segment_failure"} & set(segment.quality_flags):
                    segment_unstable += 1
                if required_russian_violations(_payload(segment)):
                    segment_language += 1
            except Exception as exc:  # pragma: no cover - production audit path
                error(
                    f"youtube_segment_invalid:{segment_path}:"
                    + safe_exception_token("youtube_segment_invalid", exc, component="luna_reenrich_audit")
                )
    counts.update(
        youtube_expected_segments=expected_segments,
        youtube_segment_files=segment_files,
        youtube_segment_unstable=segment_unstable,
        youtube_segment_language_violations=segment_language,
    )
    if segment_files != expected_segments:
        error("youtube_segment_total_mismatch")
    if segment_unstable or segment_language:
        error("youtube_segment_quality_gate_failed")
    checkpoints = list(config.YOUTUBE_CHECKPOINT_DIR.rglob("manifest.json")) if config.YOUTUBE_CHECKPOINT_DIR.exists() else []
    counts["open_youtube_checkpoints"] = len(checkpoints)
    if checkpoints:
        error("open_youtube_checkpoints")

    with sqlite3.connect(config.CARD_FTS_DB_PATH) as conn:
        cards_fts = conn.execute("SELECT COUNT(*) FROM cards_fts").fetchone()[0]
        segments_fts = conn.execute("SELECT COUNT(*) FROM youtube_segments_fts").fetchone()[0]
    with sqlite3.connect(config.SOURCE_REGISTRY_DB_PATH) as conn:
        registry = {
            "sources": conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "normalized_docs": conn.execute("SELECT COUNT(*) FROM normalized_docs").fetchone()[0],
            "enriched_cards": conn.execute("SELECT COUNT(*) FROM enriched_cards").fetchone()[0],
            "references": conn.execute('SELECT COUNT(*) FROM "references"').fetchone()[0],
            "references_with_urls": conn.execute("SELECT COUNT(*) FROM \"references\" WHERE url IS NOT NULL AND TRIM(url) <> ''").fetchone()[0],
        }
    counts.update(cards_fts=cards_fts, youtube_segments_fts=segments_fts)
    counts.update({f"registry_{key}": value for key, value in registry.items()})
    if cards_fts != len(all_cards) or segments_fts != expected_segments:
        error("fts_card_or_segment_count_mismatch")
    if registry["normalized_docs"] != counts["normalized_documents"] or registry["enriched_cards"] != len(all_cards):
        error("registry_count_mismatch")
    if registry["sources"] != counts["normalized_documents"] + expected_youtube_sources:
        error("registry_source_count_mismatch")
    if registry["references"] != registry["references_with_urls"]:
        error("registry_reference_without_url")

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "expected_model": EXPECTED_MODEL,
        "counts": counts,
        "quality_flags": dict(quality),
        "errors": errors,
        "passed": not errors,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="unique output directory name")
    parser.add_argument("--manifest", type=Path, help="accepted_manifest_v2 used for release identity")
    parser.add_argument(
        "--stdout-only",
        action="store_true",
        help="run the audit without creating an output directory or files",
    )
    args = parser.parse_args()
    run_id = args.run_id
    preflight = None
    if args.manifest:
        preflight = preflight_release_identity(run_id, args.manifest)
    elif not args.stdout_only:
        parser.error("--manifest is required when writing an audit report")
    runs_root = safe_resolve_release_path(RUNS_ROOT, label="luna_audit_runs_root")
    run_dir = safe_resolve_release_path(
        RUNS_ROOT / run_id,
        label="luna_audit_output_path",
    )
    try:
        run_dir.relative_to(runs_root)
    except ValueError:
        raise ValueError("luna audit run directory escapes runs root") from None
    result = _audit(run_id)
    result["schema_version"] = "luna_reenrich_audit_v1"
    result["release_schema_version"] = RELEASE_EVIDENCE_SCHEMA
    result["command"] = "python scripts/luna_reenrich_audit.py " + " ".join(sys.argv[1:])
    if preflight is not None:
        identity = preflight.identity
        result = stamp_report(
            result,
            identity,
            result["command"],
            producer="scripts.luna_reenrich_audit",
            producer_schema_version="luna_reenrich_audit_v1",
            source_inputs={
                "manifest_sha256": identity["manifest_sha256"],
                "corpus_identity_sha256": identity["corpus_identity_sha256"],
            },
            command_argv=["python", "scripts/luna_reenrich_audit.py", *sys.argv[1:]],
        )
    assert_no_release_secrets(canonical_json(result), label="luna_audit_report")
    if args.stdout_only:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1

    if run_dir.exists():
        raise RuntimeError(f"Audit run directory already exists: {run_dir}")
    lines = [
        "# Luna Enriched Corpus Acceptance",
        "",
        f"- passed: `{result['passed']}`",
        f"- generated_at: `{result['generated_at']}`",
        f"- expected_model: `{EXPECTED_MODEL}`",
        "",
        "## Counts",
        "",
    ]
    lines.extend(f"- {key}: `{value}`" for key, value in result["counts"].items())
    lines.extend(["", "## Quality flags", ""])
    lines.extend(f"- {key}: `{value}`" for key, value in sorted(result["quality_flags"].items()))
    lines.extend(["", "## Errors", ""])
    lines.extend(f"- {error}" for error in result["errors"] or ["none"])
    markdown = "\n".join(lines) + "\n"
    assert_no_release_secrets(markdown, label="luna_audit_markdown")
    run_dir.mkdir(parents=True)
    atomic_write_release_json(
        run_dir / "corpus_acceptance.json", result, label="luna_audit_report"
    )
    atomic_write_release_text(
        run_dir / "corpus_acceptance.md", markdown, label="luna_audit_markdown"
    )
    print(json.dumps({"passed": result["passed"]}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(run_release_cli(main, component="luna_reenrich_audit"))
