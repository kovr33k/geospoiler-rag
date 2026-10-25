"""Isolated Enriched v2 pilot runner.

The default mode is read-only: it selects representative normalized posts and
prints a manifest. Pass --run explicitly to copy the selected sources into an
isolated workspace, run the production v2 enrichment pipeline there, rebuild
temporary retrieval indexes, and write validation/recall reports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from geospoiler import config  # noqa: E402
from geospoiler.config import PROJECT_ROOT as PROJECT_DIR
from geospoiler.contracts.models import (
    EnrichedCardV2,
    NormalizedMeta,  # noqa: E402
)
from geospoiler.ingestion.enricher.chunker import (
    chunk_text,
    needs_chunking,  # noqa: E402
)
from geospoiler.ingestion.enricher.content_classifier import classify_content  # noqa: E402
from geospoiler.ingestion.enricher.pipeline import (
    EnrichmentStats,
    _build_source_id,
    enrich_all,  # noqa: E402
)
from geospoiler.ingestion.enricher.preprocessor import preprocess  # noqa: E402
from geospoiler.ingestion.enricher.triage import (
    TRIAGE_KEEP,
    auto_triage,  # noqa: E402
)
from geospoiler.ingestion.enricher.youtube_pipeline import (
    _episode_card_path,
    _episode_source_id,
    _iter_dedicated_sources,
    _looks_like_legacy_youtube_document,
    _safe_component,
    _source_fingerprint,
    _validate_generation,
    load_dedicated_youtube_artifact,
)
from geospoiler.ingestion.enricher.youtube_segmenter import (
    SEGMENT_TARGET_CHARS,
    build_segment_specs,
    needs_youtube_segments,
)
from geospoiler.providers import llm_backend  # noqa: E402
from geospoiler.retrieval.cards import shadow_search  # noqa: E402
from geospoiler.retrieval.cards.card_fts import (
    list_youtube_segment_ids,
    rebuild_card_index,
    rebuild_youtube_segment_index,
    search_card_index,
)
from geospoiler.sources.registry import rebuild_source_registry  # noqa: E402

DEFAULT_LIMIT = 12
MIN_LIMIT = 10
MAX_LIMIT = 20
PILOT_BASE_DIR = PROJECT_DIR / "artifacts" / "enriched_v2_pilot"
LEGACY_FIELDS = {
    "broll",
    "chunks",
    "dedup",
    "key_facts",
    "noise",
    "query_aliases",
    "triage",
    "visual",
}
GENERIC_QUERIES = {
    "analysis",
    "event",
    "events",
    "government",
    "international relations",
    "news",
    "politics",
    "war",
    "анализ",
    "война",
    "геополитика",
    "государство",
    "международные отношения",
    "мир",
    "новости",
    "политика",
    "событие",
    "события",
    "страна",
}
GENERIC_SINGLE_QUERIES = {
    "analysis",
    "event",
    "events",
    "government",
    "news",
    "politics",
    "state",
    "war",
    "\u0430\u043d\u0430\u043b\u0438\u0437",
    "\u0432\u043e\u0439\u043d\u0430",
    "\u0433\u0435\u043e\u043f\u043e\u043b\u0438\u0442\u0438\u043a\u0430",
    "\u0433\u043e\u0441\u0443\u0434\u0430\u0440\u0441\u0442\u0432\u043e",
    "\u043d\u043e\u0432\u043e\u0441\u0442\u0438",
    "\u043f\u043e\u043b\u0438\u0442\u0438\u043a\u0430",
    "\u043f\u0440\u0430\u0432\u0438\u0442\u0435\u043b\u044c\u0441\u0442\u0432\u043e",
    "\u0441\u043e\u0431\u044b\u0442\u0438\u0435",
    "\u0441\u043e\u0431\u044b\u0442\u0438\u044f",
    "\u0441\u0442\u0440\u0430\u043d\u0430",
}
SELF_RECALL_TOP_K = 10
DEFAULT_GOLDEN_TOP_K = 20


@dataclass(frozen=True)
class PilotCandidate:
    txt_path: Path
    meta_path: Path
    relative_txt: str
    relative_meta: str
    content_type: str
    char_count: int
    body_char_count: int
    traits: tuple[str, ...]
    youtube_video_ids: tuple[str, ...]
    youtube_long_video_ids: tuple[str, ...]
    estimated_llm_calls: int
    estimated_llm_calls_with_repair: int

    def public_dict(self) -> dict[str, Any]:
        return {
            "normalized_file": self.relative_txt,
            "meta_file": self.relative_meta,
            "content_type": self.content_type,
            "char_count": self.char_count,
            "body_char_count": self.body_char_count,
            "traits": list(self.traits),
            "youtube_video_ids": list(self.youtube_video_ids),
            "youtube_long_video_ids": list(self.youtube_long_video_ids),
            "nominal_model_calls": self.estimated_llm_calls,
            "theoretical_max_model_calls_with_one_repair": self.estimated_llm_calls_with_repair,
            "theoretical_max_http_requests_including_400_fallback": (
                2 * self.estimated_llm_calls_with_repair
            ),
        }


@dataclass(frozen=True)
class _PilotYouTubeArtifact:
    video_id: str
    transcript_text: str
    duration_seconds: float | None
    metadata_path: Path
    text_path: Path
    cues_path: Path
    metadata: dict[str, Any]


@dataclass(frozen=True)
class PilotPaths:
    pilot_dir: Path
    workspace: Path
    normalized_dir: Path
    youtube_normalized_dir: Path
    enriched_dir: Path
    youtube_segments_dir: Path
    review_queue_dir: Path
    state_dir: Path
    indexes_dir: Path
    card_fts_db: Path
    source_registry_db: Path
    report_json: Path
    report_md: Path
    media_cache_dir: Path
    media_visual_dir: Path
    transcription_dir: Path
    instagram_cache_dir: Path
    codex_runtime_dir: Path
    log_dir: Path

    @classmethod
    def build(cls, pilot_dir: Path) -> PilotPaths:
        pilot_dir = pilot_dir.resolve()
        workspace = pilot_dir / "workspace"
        output_dir = workspace / "output"
        indexes_dir = pilot_dir / "indexes"
        state_dir = workspace / "state"
        return cls(
            pilot_dir=pilot_dir,
            workspace=workspace,
            normalized_dir=output_dir / "normalized",
            youtube_normalized_dir=output_dir / "normalized_youtube",
            enriched_dir=output_dir / "enriched",
            youtube_segments_dir=output_dir / "enriched_segments",
            review_queue_dir=output_dir / "review_queue",
            state_dir=state_dir,
            indexes_dir=indexes_dir,
            card_fts_db=indexes_dir / "card_fts.sqlite",
            source_registry_db=indexes_dir / "source_registry.sqlite",
            report_json=pilot_dir / "report.json",
            report_md=pilot_dir / "report.md",
            media_cache_dir=workspace / "media_cache",
            media_visual_dir=output_dir / "visuals",
            transcription_dir=output_dir / "transcripts",
            instagram_cache_dir=output_dir / "instagram_cache",
            codex_runtime_dir=state_dir / "codex_runtime",
            log_dir=workspace / "logs",
        )


def scan_candidates(normalized_dir: Path) -> list[PilotCandidate]:
    """Read normalized metadata/text without modifying the source archive."""
    normalized_dir = normalized_dir.resolve()
    candidates: list[PilotCandidate] = []
    if not normalized_dir.exists():
        return candidates

    for txt_path in sorted(normalized_dir.rglob("*.txt"), key=lambda item: item.as_posix()):
        meta_path = txt_path.with_suffix(".meta.json")
        if not meta_path.exists():
            continue
        try:
            text = txt_path.read_text(encoding="utf-8")
            meta_raw = json.loads(meta_path.read_text(encoding="utf-8"))
            NormalizedMeta.model_validate(meta_raw)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(meta_raw, dict):
            continue

        content_type = classify_content(meta_raw, text)
        triage_status, _ = auto_triage(content_type, meta_raw, text)
        processed = preprocess(text)
        if triage_status != TRIAGE_KEEP or processed.body_char_count < 20:
            continue

        traits, youtube_video_ids, youtube_long_video_ids = _candidate_traits(
            meta_raw,
            content_type,
            processed,
            meta_path,
        )
        base_calls = _estimate_llm_calls(processed.clean_text, processed.body_char_count)
        candidates.append(
            PilotCandidate(
                txt_path=txt_path,
                meta_path=meta_path,
                relative_txt=txt_path.relative_to(normalized_dir).as_posix(),
                relative_meta=meta_path.relative_to(normalized_dir).as_posix(),
                content_type=content_type,
                char_count=len(text),
                body_char_count=processed.body_char_count,
                traits=traits,
                youtube_video_ids=youtube_video_ids,
                youtube_long_video_ids=youtube_long_video_ids,
                estimated_llm_calls=base_calls,
                estimated_llm_calls_with_repair=base_calls + (1 if base_calls else 0),
            )
        )
    return candidates


def select_representative_posts(
    normalized_dir: Path,
    limit: int = DEFAULT_LIMIT,
    *,
    require_long_youtube: bool = False,
) -> list[PilotCandidate]:
    """Select a deterministic, diverse pilot sample from eligible normalized posts."""
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    candidates = scan_candidates(normalized_dir)
    selected: list[PilotCandidate] = []
    selected_paths: set[str] = set()

    youtube_candidates = [
        item
        for item in candidates
        if "youtube" in item.traits
        and bool(item.youtube_video_ids)
        and (not require_long_youtube or "youtube_long" in item.traits)
    ]
    if not youtube_candidates:
        requirement = " long-form" if require_long_youtube else ""
        raise ValueError(
            f"Pilot selection requires at least one eligible{requirement} YouTube transcript "
            "with a dedicated source artifact"
        )
    youtube_choice = min(
        youtube_candidates,
        key=lambda item: (
            abs(item.body_char_count - (SEGMENT_TARGET_CHARS if require_long_youtube else 5_000)),
            _stable_key(item.relative_txt),
        ),
    )
    selected.append(youtube_choice)
    selected_paths.add(youtube_choice.relative_txt)

    slots: tuple[tuple[str, int], ...] = (
        ("telegram_short", 300),
        ("telegram_medium", 1_500),
        ("telegram_long", 5_000),
        ("forward", 1_500),
        ("instagram", 1_500),
        ("web", 2_500),
        ("ignored_media", 1_000),
    )
    for trait, target_length in slots:
        eligible = [
            item
            for item in candidates
            if trait in item.traits
            and "youtube" not in item.traits
            and item.relative_txt not in selected_paths
        ]
        if not eligible or len(selected) >= limit:
            continue
        chosen = min(
            eligible,
            key=lambda item: (
                abs(item.body_char_count - target_length),
                _stable_key(item.relative_txt),
            ),
        )
        selected.append(chosen)
        selected_paths.add(chosen.relative_txt)

    trait_counts: dict[str, int] = {}
    for item in selected:
        for trait in item.traits:
            trait_counts[trait] = trait_counts.get(trait, 0) + 1

    remaining = [
        item
        for item in candidates
        if item.relative_txt not in selected_paths
        and "youtube" not in item.traits
    ]
    while remaining and len(selected) < limit:
        chosen = min(
            remaining,
            key=lambda item: (
                sum(trait_counts.get(trait, 0) for trait in item.traits),
                abs(item.body_char_count - 1_500),
                _stable_key(item.relative_txt),
            ),
        )
        selected.append(chosen)
        remaining.remove(chosen)
        for trait in chosen.traits:
            trait_counts[trait] = trait_counts.get(trait, 0) + 1

    if sum("youtube" in item.traits for item in selected) != 1:
        raise ValueError("Pilot selection must contain exactly one YouTube transcript")
    return selected


def assert_isolated_paths(paths: PilotPaths, source_normalized_dir: Path) -> None:
    """Reject any layout that could write to a live project location."""
    pilot_dir = paths.pilot_dir.resolve()
    source_normalized_dir = source_normalized_dir.resolve()
    write_targets = (
        paths.workspace,
        paths.normalized_dir,
        paths.youtube_normalized_dir,
        paths.enriched_dir,
        paths.youtube_segments_dir,
        paths.review_queue_dir,
        paths.state_dir,
        paths.indexes_dir,
        paths.card_fts_db,
        paths.source_registry_db,
        paths.report_json,
        paths.report_md,
        paths.media_cache_dir,
        paths.media_visual_dir,
        paths.transcription_dir,
        paths.instagram_cache_dir,
        paths.codex_runtime_dir,
        paths.log_dir,
    )
    for target in write_targets:
        if not target.resolve().is_relative_to(pilot_dir):
            raise ValueError(f"Pilot write path escapes pilot directory: {target}")

    protected = tuple(
        _resolved_path(path)
        for path in (
            source_normalized_dir,
            config.OUTPUT_DIR,
            config.NORMALIZED_DIR,
            config.YOUTUBE_NORMALIZED_DIR,
            config.ENRICHED_DIR,
            config.YOUTUBE_SEGMENTS_DIR,
            config.STATE_DIR,
            config.RAG_STORAGE_DIR,
            config.CARD_FTS_DB_PATH,
            config.SOURCE_REGISTRY_DB_PATH,
            config.MEDIA_CACHE_DIR,
            config.MEDIA_VISUAL_DIR,
            config.TRANSCRIPTION_DIR,
            config.INSTAGRAM_CACHE_DIR,
            config.CODEX_RUNTIME_DIR,
            config.LOG_DIR,
        )
    )
    for live_path in protected:
        if _paths_overlap(pilot_dir, live_path):
            raise ValueError(f"Pilot directory overlaps protected live path: {live_path}")


def run_live_pilot(
    selected: Sequence[PilotCandidate],
    paths: PilotPaths,
    source_normalized_dir: Path,
    run_id: str,
    curated_golden: dict[str, Any] | None = None,
    golden_top_k: int = DEFAULT_GOLDEN_TOP_K,
) -> tuple[dict[str, Any], bool]:
    """Run the real v2 pipeline only inside the isolated pilot workspace."""
    assert_isolated_paths(paths, source_normalized_dir)
    if paths.pilot_dir.exists() and any(paths.pilot_dir.iterdir()):
        raise FileExistsError(
            f"Pilot directory is not empty: {paths.pilot_dir}. Use a new --run-id or --output-dir."
        )

    copied_youtube_artifacts = _prepare_workspace(selected, paths)
    report = _base_report(run_id, selected, paths, curated_golden, golden_top_k)
    report["youtube_artifacts_copied"] = copied_youtube_artifacts
    report["media"] = _audit_media(selected, paths)
    success = False

    try:
        with isolated_pipeline_config(paths):
            _preflight_youtube_discovery(selected)
            stats = enrich_all(force=True)
            report["enrichment_stats"] = _enrichment_stats_dict(stats)
            index_stats, index_errors = _rebuild_isolated_indexes(paths)
            report["indexes"] = index_stats
            report["errors"].extend(index_errors)

            card_results = _validate_produced_cards(selected, paths)
            report["cards"] = card_results
            report["youtube_long_form"] = _validate_long_youtube_outputs(
                selected,
                paths,
                report,
            )
            report["recall"] = _run_recall_checks(
                card_results,
                paths,
                curated_golden=curated_golden,
                golden_top_k=golden_top_k,
            )

        success = _report_passes(report, selected)
    except Exception as exc:  # Keep a report for batch-level failures.
        report["errors"].append(_safe_error(exc))
        success = False

    report["passed"] = success
    report["completed_at"] = datetime.now(UTC).isoformat()
    write_report(report, paths)
    return report, success


def _preflight_youtube_discovery(selected: Sequence[PilotCandidate]) -> None:
    """Fail before LLM work if production cannot discover copied YouTube sources."""
    expected_video_ids = {
        video_id
        for item in selected
        for video_id in item.youtube_video_ids
    }
    if not expected_video_ids:
        return

    discovery_errors: list[tuple[Path, str]] = []
    discovered = list(_iter_dedicated_sources(None, discovery_errors))
    discovered_by_id: dict[str, list[dict[str, Any]]] = {}
    for source in discovered:
        discovered_by_id.setdefault(str(source.get("video_id") or ""), []).append(source)

    missing = sorted(video_id for video_id in expected_video_ids if not discovered_by_id.get(video_id))
    duplicate = sorted(
        video_id
        for video_id in expected_video_ids
        if len(discovered_by_id.get(video_id, [])) > 1
    )
    if missing or duplicate or discovery_errors:
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if duplicate:
            details.append("not_exactly_once=" + ",".join(duplicate))
        if discovery_errors:
            details.append(
                "errors="
                + "; ".join(f"{path}: {reason}" for path, reason in discovery_errors)
            )
        raise ValueError("YouTube discovery preflight failed: " + " | ".join(details))


@contextmanager
def isolated_pipeline_config(paths: PilotPaths) -> Iterator[None]:
    """Temporarily redirect every relevant production path to the pilot workspace."""
    import geospoiler.ingestion.enricher.pipeline as enrichment_pipeline

    output_dir = paths.workspace / "output"
    replacements = {
        "PROJECT_ROOT": paths.workspace,
        "OUTPUT_DIR": output_dir,
        "NORMALIZED_DIR": paths.normalized_dir,
        "YOUTUBE_NORMALIZED_DIR": paths.youtube_normalized_dir,
        "ENRICHED_DIR": paths.enriched_dir,
        "YOUTUBE_SEGMENTS_DIR": paths.youtube_segments_dir,
        "REVIEW_QUEUE_DIR": paths.review_queue_dir,
        "STATE_DIR": paths.state_dir,
        "YOUTUBE_CHECKPOINT_DIR": paths.state_dir / "youtube_checkpoints",
        "CARD_FTS_DB_PATH": paths.card_fts_db,
        "SOURCE_REGISTRY_DB_PATH": paths.source_registry_db,
        "RAG_STORAGE_DIR": paths.workspace / "rag_storage",
        "MEDIA_CACHE_DIR": paths.media_cache_dir,
        "MEDIA_VISUAL_DIR": paths.media_visual_dir,
        "TRANSCRIPTION_DIR": paths.transcription_dir,
        "INSTAGRAM_CACHE_DIR": paths.instagram_cache_dir,
        "CODEX_RUNTIME_DIR": paths.codex_runtime_dir,
        "LOG_DIR": paths.log_dir,
    }
    previous = {name: getattr(config, name) for name in replacements}
    previous_progress_file = enrichment_pipeline._PROGRESS_FILE
    try:
        for name, value in replacements.items():
            setattr(config, name, value)
        enrichment_pipeline._PROGRESS_FILE = paths.state_dir / "enrichment_progress.json"
        yield
    finally:
        enrichment_pipeline._PROGRESS_FILE = previous_progress_file
        for name, value in previous.items():
            setattr(config, name, value)


def write_report(report: dict[str, Any], paths: PilotPaths) -> None:
    """Write machine-readable and compact human-readable reports."""
    paths.pilot_dir.mkdir(parents=True, exist_ok=True)
    paths.report_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    stats = report.get("enrichment_stats") or {}
    selected = report.get("selected") or []
    cards = report.get("cards") or []
    recall = report.get("recall") or {}
    self_recall = recall.get("self_recall") or {}
    self_checks = self_recall.get("checks") or []
    curated = recall.get("curated_golden") or {}
    curated_checks = curated.get("checks") or []
    estimates = report.get("call_estimates") or {}
    lines = [
        "# Enriched v2 pilot report",
        "",
        f"- Run ID: `{report.get('run_id', '')}`",
        f"- Passed: **{bool(report.get('passed'))}**",
        f"- Selected: {len(selected)}",
        f"- Enriched: {stats.get('enriched', 0)}",
        f"- Failed: {stats.get('failed', 0)}",
        f"- Partial: {stats.get('partial', 0)}",
        f"- Repaired: {stats.get('repaired', 0)}",
        f"- Self recall@{self_recall.get('top_k', SELF_RECALL_TOP_K)} passed: "
        f"{sum(1 for item in self_checks if item.get('passed'))}/{len(self_checks)}",
        f"- Curated golden: {'configured' if curated.get('configured') else 'not configured'}",
        f"- Nominal model calls: {estimates.get('nominal_model_calls', 0)}",
        "- Theoretical max model calls with one repair per card: "
        f"{estimates.get('theoretical_max_model_calls_with_one_repair_per_card', 0)}",
        "- Theoretical max HTTP requests including HTTP 400 fallback: "
        f"{estimates.get('theoretical_max_http_requests_including_400_fallback', 0)}",
        "",
        "## Versions",
        "",
        f"- Schema: `{report.get('versions', {}).get('schema_version', '')}`",
        f"- Prompt: `{report.get('versions', {}).get('prompt_version', '')}`",
        f"- Model: `{report.get('versions', {}).get('model', '')}`",
        "",
        "## Selected files",
        "",
        "| File | Type | Body chars | Traits | Nominal model calls |",
        "| --- | --- | ---: | --- | ---: |",
    ]
    for item in selected:
        traits = ", ".join(item.get("traits") or [])
        lines.append(
            f"| `{item.get('normalized_file', '')}` | `{item.get('content_type', '')}` | "
            f"{item.get('body_char_count', 0)} | {traits} | {item.get('nominal_model_calls', 0)} |"
        )
    lines.extend(
        [
            "",
            "## Card checks",
            "",
            "| Source ID | Status | Static checks | Quality diagnostics |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in cards:
        checks = item.get("checks") or {}
        passed_checks = sum(1 for value in checks.values() if value is True)
        diagnostics = list(item.get("quality_flags") or [])
        diagnostics.extend(item.get("extraction_issues") or [])
        diagnostic_text = "; ".join(str(value).replace("|", "/") for value in diagnostics)
        lines.append(
            f"| `{item.get('source_id', '')}` | {item.get('status', '')} | "
            f"{passed_checks}/{len(checks)} passed | {diagnostic_text} |"
        )
    media = report.get("media") or {}
    lines.extend(
        [
            "",
            "## Media gate",
            "",
            f"- Passed: **{bool(media.get('passed'))}**",
            f"- Native media items: {media.get('native_media_items', 0)}",
            f"- Images checked: {media.get('images_checked', 0)}",
            f"- YouTube sources checked: {media.get('youtube_sources_checked', 0)}",
        ]
    )
    for issue in media.get("issues") or []:
        lines.append(f"- Issue: `{str(issue).replace('|', '/')}`")
    long_form = report.get("youtube_long_form") or {}
    lines.extend(
        [
            "",
            "## Long YouTube validation",
            "",
            f"- Required: **{bool(long_form.get('required'))}**",
            f"- Passed: **{bool(long_form.get('passed'))}**",
        ]
    )
    for name, passed in (long_form.get("checks") or {}).items():
        lines.append(f"- `{name}`: {bool(passed)}")
    lines.extend(
        [
            "",
            f"## Self recall@{self_recall.get('top_k', SELF_RECALL_TOP_K)}",
            "",
            "Self-derived checks verify that each card can retrieve itself; they are not a curated golden set.",
            "",
            "| Query | Must find | Rank | Passed |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for item in self_checks:
        lines.append(
            f"| `{item.get('query', '')}` | `{item.get('must_find_source_id', '')}` | "
            f"{item.get('rank') or ''} | {bool(item.get('passed'))} |"
        )
    lines.extend(["", "## Curated golden", ""])
    if not curated.get("configured"):
        lines.append("Not configured. No self-derived expectation is reported as golden.")
    else:
        lines.extend(
            [
                f"Manifest: `{curated.get('manifest_path', '')}`",
                "",
                f"Top K: {curated.get('top_k', DEFAULT_GOLDEN_TOP_K)}",
                "",
                "| Query | Must find | Passed |",
                "| --- | --- | --- |",
            ]
        )
        for item in curated_checks:
            must_find = ", ".join(item.get("must_find_source_ids") or [])
            lines.append(
                f"| `{item.get('query', '')}` | `{must_find}` | {bool(item.get('passed'))} |"
            )
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in report["errors"])
    paths.report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Execute live LLM enrichment in the isolated workspace")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Pilot sample size (10-20, default: 12)")
    parser.add_argument("--run-id", help="Stable run label; defaults to a UTC timestamp")
    parser.add_argument("--output-dir", type=Path, help="Exact pilot directory; default: artifacts/enriched_v2_pilot/<run-id>")
    parser.add_argument("--normalized-dir", type=Path, default=Path(config.NORMALIZED_DIR), help="Read-only normalized source directory")
    parser.add_argument(
        "--require-long-youtube",
        action="store_true",
        help="Require the selected YouTube sample to cross the real segmentation threshold",
    )
    parser.add_argument(
        "--golden-manifest",
        type=Path,
        help="Optional curated JSON queries with must_find_source_ids or selected normalized files",
    )
    parser.add_argument(
        "--golden-top-k",
        type=int,
        default=DEFAULT_GOLDEN_TOP_K,
        help=f"Top-K cutoff for curated golden checks (default: {DEFAULT_GOLDEN_TOP_K})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console_encoding()
    args = build_parser().parse_args(argv)
    if not MIN_LIMIT <= args.limit <= MAX_LIMIT:
        print(f"--limit must be between {MIN_LIMIT} and {MAX_LIMIT}", file=sys.stderr)
        return 2
    if args.golden_top_k < 1:
        print("--golden-top-k must be at least 1", file=sys.stderr)
        return 2

    try:
        run_id = _validated_run_id(args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
        normalized_dir = args.normalized_dir.resolve()
        selected = select_representative_posts(
            normalized_dir,
            args.limit,
            require_long_youtube=args.require_long_youtube,
        )
        pilot_dir = (args.output_dir or (PILOT_BASE_DIR / run_id)).resolve()
        paths = PilotPaths.build(pilot_dir)
        assert_isolated_paths(paths, normalized_dir)
        curated_golden = _load_curated_golden_manifest(
            args.golden_manifest,
            selected,
            paths,
        )
    except (OSError, ValueError) as exc:
        print(f"Pilot setup failed: {_safe_error(exc)}", file=sys.stderr)
        return 2

    _print_selection(selected, run_id, paths, dry_run=not args.run)
    if len(selected) < args.limit:
        print(f"Only {len(selected)} eligible posts found for requested limit {args.limit}.", file=sys.stderr)
        if args.run:
            return 1
    if not args.run:
        print("Dry-run only. No files were written and no LLM was called. Add --run to execute.")
        return 0

    try:
        _, success = run_live_pilot(
            selected,
            paths,
            normalized_dir,
            run_id,
            curated_golden=curated_golden,
            golden_top_k=args.golden_top_k,
        )
    except (OSError, ValueError) as exc:
        print(f"Pilot failed before execution: {_safe_error(exc)}", file=sys.stderr)
        return 1
    print(f"Pilot reports: {paths.report_json} and {paths.report_md}")
    return 0 if success else 1


def _candidate_traits(
    meta: dict[str, Any],
    content_type: str,
    processed: Any,
    meta_path: Path,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    traits: set[str] = set()
    youtube_video_ids: tuple[str, ...] = ()
    youtube_long_video_ids: tuple[str, ...] = ()
    if meta.get("is_forward"):
        traits.add("forward")
    if meta.get("youtube_urls"):
        traits.add("youtube")
        profiles = _youtube_profiles(
            meta,
            meta_path,
        )
        youtube_video_ids = tuple(video_id for video_id, _, _ in profiles)
        youtube_long_video_ids = tuple(
            video_id
            for video_id, youtube_text, duration_seconds in profiles
            if needs_youtube_segments(youtube_text, duration_seconds)
        )
        if youtube_long_video_ids:
            traits.add("youtube_long")
    if meta.get("instagram_urls"):
        traits.add("instagram")
    if meta.get("web_urls"):
        traits.add("web")
    if processed.ignored_blocks:
        traits.add("ignored_media")
    if content_type in {"telegram_post", "telegram_forward"}:
        if processed.body_char_count < 500:
            traits.add("telegram_short")
        elif processed.body_char_count <= 3_000:
            traits.add("telegram_medium")
        else:
            traits.add("telegram_long")
    return tuple(sorted(traits)), youtube_video_ids, youtube_long_video_ids


def _youtube_profiles(
    meta: dict[str, Any],
    meta_path: Path,
) -> list[tuple[str, str, float | None]]:
    """Return profiles only for copyable dedicated YouTube artifacts."""
    dedicated: list[tuple[str, str, float | None]] = []
    entries = meta.get("youtube_sources") or []
    urls_value = meta.get("youtube_urls") or []
    urls = [str(url) for url in urls_value] if isinstance(urls_value, list) else []
    expected_video_ids = {
        video_id
        for video_id in (_youtube_id(url) for url in urls)
        if video_id != "unknown"
    }
    if isinstance(entries, list):
        for entry in entries:
            try:
                artifact = _load_dedicated_youtube_artifact(
                    entry,
                    expected_video_ids,
                    meta_path.parent,
                )
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                continue
            dedicated.append(
                (artifact.video_id, artifact.transcript_text, artifact.duration_seconds)
            )
    return dedicated


def _as_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _estimate_llm_calls(clean_text: str, body_char_count: int) -> int:
    if body_char_count < 20:
        return 0
    if needs_chunking(clean_text):
        return len(chunk_text(clean_text)) + 1
    return 1


def _stable_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _paths_overlap(first: Path, second: Path) -> bool:
    first = _resolved_path(first)
    second = _resolved_path(second)
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _resolved_path(value: str | Path) -> Path:
    """Resolve traversal and every existing symlink component without requiring the leaf."""
    return Path(value).expanduser().resolve(strict=False)


def _validated_run_id(value: str) -> str:
    if not value or value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
        raise ValueError("run-id must use 1-80 ASCII letters, digits, dots, underscores, or hyphens")
    return value


def _prepare_workspace(selected: Sequence[PilotCandidate], paths: PilotPaths) -> int:
    for directory in (
        paths.normalized_dir,
        paths.youtube_normalized_dir,
        paths.enriched_dir,
        paths.youtube_segments_dir,
        paths.review_queue_dir,
        paths.state_dir,
        paths.indexes_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    copied_artifacts = 0
    for item in selected:
        txt_target = paths.normalized_dir / item.relative_txt
        meta_target = paths.normalized_dir / item.relative_meta
        txt_target.parent.mkdir(parents=True, exist_ok=True)
        meta_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.txt_path, txt_target)
        shutil.copy2(item.meta_path, meta_target)
        copied_artifacts += _copy_youtube_artifacts(item, paths)
    return copied_artifacts


def _copy_youtube_artifacts(item: PilotCandidate, paths: PilotPaths) -> int:
    """Copy dedicated artifacts and rewrite their paths for the isolated workspace."""
    try:
        source_meta = json.loads(item.meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return 0
    if not isinstance(source_meta, dict):
        return 0
    entries = source_meta.get("youtube_sources") or []
    if not isinstance(entries, list):
        return 0
    copied = 0
    channel_component = _safe_component(
        source_meta.get("channel_id") or source_meta.get("channel_name") or "youtube"
    )
    message_component = _safe_component(source_meta.get("message_id") or item.txt_path.stem)
    target_root = paths.youtube_normalized_dir.resolve()
    target_dir = target_root / channel_component / message_component
    if not target_dir.resolve().is_relative_to(target_root):
        raise ValueError(f"YouTube artifact destination escapes pilot workspace: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    urls_value = source_meta.get("youtube_urls") or []
    source_urls = [str(url) for url in urls_value] if isinstance(urls_value, list) else []
    expected_video_ids = set(item.youtube_video_ids)
    copied_ids: set[str] = set()
    source_video_ids = {
        video_id
        for video_id in (_youtube_id(url) for url in source_urls)
        if video_id != "unknown"
    }
    for entry in entries:
        source_meta_path: object = "unknown"
        try:
            artifact = _load_dedicated_youtube_artifact(
                entry,
                source_video_ids,
                item.meta_path.parent,
            )
            if artifact.video_id in copied_ids:
                continue
            source_meta_path = artifact.metadata_path
            video_component = _safe_component(artifact.video_id)
            artifact_dir = target_dir / video_component
            if not artifact_dir.resolve().is_relative_to(target_root):
                raise ValueError(f"YouTube artifact destination escapes pilot workspace: {artifact_dir}")
            artifact_dir.mkdir(parents=True, exist_ok=True)
            target_text = artifact_dir / f"{video_component}.youtube.txt"
            target_cues = artifact_dir / f"{video_component}.youtube.cues.json" if artifact.cues_path.is_file() else None
            target_meta = artifact_dir / f"{video_component}.youtube.meta.json"
            shutil.copy2(artifact.text_path, target_text)
            if target_cues is not None:
                shutil.copy2(artifact.cues_path, target_cues)
            artifact_meta = dict(artifact.metadata)
            artifact_meta["transcript_path"] = str(target_text.relative_to(paths.workspace))
            artifact_meta["cues_path"] = (
                str(target_cues.relative_to(paths.workspace)) if target_cues else ""
            )
            artifact_meta["telegram_source"] = {
                "channel_name": source_meta.get("channel_name"),
                "channel_id": source_meta.get("channel_id"),
                "message_id": source_meta.get("message_id"),
                "date": source_meta.get("date"),
                "post_url": source_meta.get("post_url"),
            }
            target_meta.write_text(
                json.dumps(artifact_meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            copied += 1
            copied_ids.add(artifact.video_id)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            print(
                f"Skipping invalid YouTube pilot artifact {source_meta_path}: {_safe_error(exc)}",
                file=sys.stderr,
            )
            continue
    missing_ids = expected_video_ids - copied_ids
    if missing_ids:
        raise ValueError(
            "Selected YouTube artifacts were not copied: "
            + ", ".join(sorted(missing_ids))
        )
    return copied


def _resolve_artifact_path(value: object, base_dir: Path) -> Path:
    if not value:
        return base_dir / "missing"
    path = Path(str(value))
    if path.is_absolute():
        return path
    candidates = [config.PROJECT_ROOT / path, base_dir / path]
    existing: dict[Path, Path] = {}
    for candidate in candidates:
        if candidate.is_file():
            existing[candidate.resolve()] = candidate
    if len(existing) > 1:
        raise ValueError(f"Ambiguous relative YouTube artifact path: {value}")
    return next(iter(existing.values())) if existing else candidates[0]


def _load_dedicated_youtube_artifact(
    entry: object,
    expected_video_ids: set[str],
    base_dir: Path,
) -> _PilotYouTubeArtifact:
    if not isinstance(entry, dict):
        raise ValueError("YouTube source entry must be an object")
    metadata_path = _resolve_artifact_path(entry.get("metadata_path"), base_dir)
    text_path = _resolve_artifact_path(entry.get("text_path"), base_dir)
    cues_path = _resolve_artifact_path(entry.get("cues_path"), base_dir)
    artifact = load_dedicated_youtube_artifact(
        metadata_path,
        text_path,
        cues_path,
        entry_video_id=str(entry.get("video_id") or "") or None,
        expected_video_ids=expected_video_ids or None,
    )
    duration = _as_float(entry.get("duration_seconds")) or _as_float(
        artifact.metadata.get("duration_seconds")
    )
    return _PilotYouTubeArtifact(
        video_id=artifact.video_id,
        transcript_text=artifact.transcript_text,
        duration_seconds=duration,
        metadata_path=artifact.metadata_path,
        text_path=artifact.text_path,
        cues_path=artifact.cues_path,
        metadata=artifact.metadata,
    )


def _base_report(
    run_id: str,
    selected: Sequence[PilotCandidate],
    paths: PilotPaths,
    curated_golden: dict[str, Any] | None = None,
    golden_top_k: int = DEFAULT_GOLDEN_TOP_K,
) -> dict[str, Any]:
    nominal_model_calls = sum(item.estimated_llm_calls for item in selected)
    max_model_calls = sum(item.estimated_llm_calls_with_repair for item in selected)
    return {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "completed_at": None,
        "passed": False,
        "versions": {
            "schema_version": config.ENRICHMENT_SCHEMA_VERSION,
            "prompt_version": config.ENRICHMENT_PROMPT_VERSION,
            "youtube_prompt_version": config.YOUTUBE_ENRICHMENT_PROMPT_VERSION,
            "model": llm_backend.active_model_for("enrichment"),
            "profile": llm_backend.active_profile(),
        },
        "isolation": {
            "pilot_dir": str(paths.pilot_dir),
            "workspace": str(paths.workspace),
            "enriched_dir": str(paths.enriched_dir),
            "card_fts_db": str(paths.card_fts_db),
            "source_registry_db": str(paths.source_registry_db),
            "lightrag_called": False,
        },
        "selected": [item.public_dict() for item in selected],
        "call_estimates": {
            "nominal_model_calls": nominal_model_calls,
            "theoretical_max_model_calls_with_one_repair_per_card": max_model_calls,
            "theoretical_max_http_requests_including_400_fallback": 2 * max_model_calls,
        },
        "enrichment_stats": {},
        "indexes": {},
        "cards": [],
        "recall": {
            "self_recall": {
                "configured": True,
                "top_k": SELF_RECALL_TOP_K,
                "checks": [],
            },
            "curated_golden": {
                "configured": curated_golden is not None,
                "manifest_path": (curated_golden or {}).get("manifest_path", ""),
                "top_k": golden_top_k,
                "checks": [],
            },
        },
        "errors": [],
    }


def _enrichment_stats_dict(stats: EnrichmentStats) -> dict[str, Any]:
    return {
        "scanned": stats.scanned,
        "enriched": stats.enriched,
        "partial": stats.partial,
        "repaired": stats.repaired,
        "failed": stats.failed,
        "skipped_review": stats.skipped_review,
        "skipped_no_meta": stats.skipped_no_meta,
        "skipped_up_to_date": stats.skipped_up_to_date,
        "partial_posts": list(stats.partial_posts),
        "by_content_type": dict(stats.by_content_type),
        "youtube_sources": stats.youtube_sources,
        "youtube_episodes": stats.youtube_episodes,
        "youtube_skipped": stats.youtube_skipped,
        "youtube_segments": stats.youtube_segments,
        "youtube_partial": stats.youtube_partial,
        "youtube_failed": stats.youtube_failed,
    }


def _rebuild_isolated_indexes(paths: PilotPaths) -> tuple[dict[str, Any], list[str]]:
    stats: dict[str, Any] = {}
    errors: list[str] = []
    builders = (
        (
            "card_fts",
            lambda: rebuild_card_index(paths.enriched_dir, paths.card_fts_db),
        ),
        (
            "youtube_segment_fts",
            lambda: rebuild_youtube_segment_index(
                paths.youtube_segments_dir,
                paths.card_fts_db,
            ),
        ),
        (
            "source_registry",
            lambda: rebuild_source_registry(
                paths.normalized_dir,
                paths.enriched_dir,
                paths.source_registry_db,
            ),
        ),
    )
    for name, builder in builders:
        try:
            result = builder()
            stats[name] = _json_safe(asdict(result))
        except Exception as exc:
            stats[name] = None
            errors.append(f"{name}: {_safe_error(exc)}")
    return stats, errors


def _validate_produced_cards(
    selected: Sequence[PilotCandidate],
    paths: PilotPaths,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in selected:
        card_kinds = _expected_card_kinds(item)
        primary_kind = "episode" if "episode" in card_kinds else card_kinds[0]
        primary_video_id = item.youtube_video_ids[0] if item.youtube_video_ids else None
        card_path = _expected_card_path_for_kind(
            item,
            paths,
            primary_kind,
            video_id=primary_video_id,
        )
        result: dict[str, Any] = {
            "normalized_file": item.relative_txt,
            "card_file": _relative_or_string(card_path, paths.pilot_dir),
            "card_files": [],
            "source_id": "",
            "content_type": item.content_type,
            "status": "missing",
            "checks": {},
            "errors": [],
        }
        all_checks: dict[str, bool] = {}
        for card_kind in card_kinds:
            if card_kind == "episode" and not item.youtube_video_ids:
                result["errors"].append(
                    "YouTube episode expected, but no dedicated transcript source was resolved"
                )
                continue
            video_ids = item.youtube_video_ids if card_kind == "episode" else (None,)
            for video_id in video_ids:
                card_path = _expected_card_path_for_kind(
                    item,
                    paths,
                    card_kind,
                    video_id=video_id,
                )
                result["card_files"].append(_relative_or_string(card_path, paths.pilot_dir))
                label = f"{card_kind}_{video_id}" if video_id else card_kind
                if not card_path.exists():
                    result["errors"].append(f"Expected {label} enriched card was not produced")
                    continue
                try:
                    raw = json.loads(card_path.read_text(encoding="utf-8"))
                    card = EnrichedCardV2.model_validate(raw)
                except Exception as exc:
                    result["errors"].append(f"{label}: {_safe_error(exc)}")
                    continue
                if card_kind == primary_kind and result["source_id"] == "":
                    result["source_id"] = card.provenance.source_id
                    result["content_type"] = card.content_type
                    result["quality_flags"] = list(card.quality_flags)
                    result["extraction_issues"] = list(card.extraction_issues)
                checks = _static_card_checks(
                    card,
                    raw,
                    item,
                    paths,
                    card_kind=card_kind,
                    video_id=video_id,
                )
                all_checks.update({f"{label}_{key}": value for key, value in checks.items()})
        result["checks"] = all_checks
        result["status"] = "valid" if not result["errors"] and all(all_checks.values()) else "failed_checks"
        results.append(result)
    return results


def _static_card_checks(
    card: EnrichedCardV2,
    raw: dict[str, Any],
    candidate: PilotCandidate,
    paths: PilotPaths,
    *,
    card_kind: str = "episode",
    video_id: str | None = None,
) -> dict[str, bool]:
    expected_source_id, expected_normalized_path, expected_content_type = _expected_card_identity(
        candidate,
        paths,
        card_kind=card_kind,
        video_id=video_id,
    )
    normalized_path = Path(card.provenance.normalized_path)
    resolved_source = (
        (paths.workspace / normalized_path).resolve()
        if not normalized_path.is_absolute()
        else normalized_path.resolve()
    )
    source_isolated = (
        resolved_source.is_relative_to(paths.normalized_dir.resolve())
        or resolved_source.is_relative_to(paths.youtube_normalized_dir.resolve())
    )
    source_resolves = source_isolated and resolved_source.is_file()
    substantive_payload = bool(card.summary.strip() or card.key_points) if candidate.body_char_count >= 30 else True
    legacy_keys = _find_legacy_keys(raw)
    expected_ignored = preprocess(candidate.txt_path.read_text(encoding="utf-8")).ignored_blocks
    semantic_blob = _semantic_blob(raw)
    ignored_not_extracted = all(
        not block.text.strip() or block.text.casefold() not in semantic_blob
        for block in expected_ignored
    )
    return {
        "strict_enriched_v2": card.schema_version == config.ENRICHMENT_SCHEMA_VERSION,
        "no_legacy_fields_or_chunks": not legacy_keys,
        "provenance_source_id_exact": card.provenance.source_id == expected_source_id,
        "provenance_normalized_path_exact": (
            card.provenance.normalized_path == expected_normalized_path
        ),
        "content_type_exact": card.content_type == expected_content_type,
        "substantive_payload": substantive_payload,
        "graph_text_nonempty": bool(card.graph_text.strip()),
        "search_text_nonempty": bool(card.search_text.strip()),
        "ignored_placeholders_not_extracted": ignored_not_extracted,
        "source_path_resolvable": source_resolves,
        "no_pipeline_failure_flags": not (
            {"extraction_unstable", "partial_segment_failure"} & set(card.quality_flags)
        ),
    }


def _expected_card_identity(
    candidate: PilotCandidate,
    paths: PilotPaths,
    *,
    card_kind: str = "episode",
    video_id: str | None = None,
) -> tuple[str, str, str]:
    meta = NormalizedMeta.model_validate_json(candidate.meta_path.read_text(encoding="utf-8"))
    source_text = candidate.txt_path.read_text(encoding="utf-8")
    relative = Path(candidate.relative_txt)
    if len(relative.parts) != 2 or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsupported normalized layout for pilot: {candidate.relative_txt}")
    channel_name = relative.parent.name
    msg_id = relative.stem
    expected_source_id = _build_source_id(meta, channel_name, msg_id)
    expected_normalized_path = str(
        (paths.normalized_dir / relative).relative_to(paths.workspace)
    )
    if meta.youtube_urls and card_kind == "episode":
        selected_video_id = video_id or _youtube_id(meta.youtube_urls[0])
        expected_source_id = f"{expected_source_id}:youtube:{selected_video_id}"
        expected_content_type = "youtube_transcript"
        source = _dedicated_source_for_candidate(candidate, selected_video_id)
        if source is not None and Path(str(source["text_path"])).resolve().is_relative_to(
            paths.workspace.resolve()
        ):
            expected_normalized_path = str(
                Path(str(source["text_path"])).relative_to(paths.workspace)
            )
        else:
            youtube_sources = meta.model_dump(mode="json").get("youtube_sources") or []
            matching_entry = next(
                (
                    entry
                    for entry in youtube_sources
                    if isinstance(entry, dict)
                    and str(entry.get("video_id") or "") == selected_video_id
                ),
                None,
            )
            source_text_path = Path(
                str((matching_entry or {}).get("text_path") or "youtube.txt")
            )
            channel = re.sub(
                r"[^\w.-]+",
                "_",
                str(meta.channel_id or meta.channel_name or relative.parent.name),
            )
            message_id = str(meta.message_id or relative.stem)
            expected_normalized_path = str(
                (
                    paths.youtube_normalized_dir
                    / channel
                    / message_id
                    / _safe_component(selected_video_id)
                    / source_text_path.name
                ).relative_to(paths.workspace)
            )
    else:
        expected_content_type = classify_content(meta.model_dump(mode="json"), source_text)
    return expected_source_id, expected_normalized_path, expected_content_type


def _expected_card_path(candidate: PilotCandidate, paths: PilotPaths) -> Path:
    """Return the primary output path for a candidate."""
    card_kind = "episode" if _has_youtube_url(candidate) else "telegram"
    return _expected_card_path_for_kind(candidate, paths, card_kind)


def _expected_card_path_for_kind(
    candidate: PilotCandidate,
    paths: PilotPaths,
    card_kind: str,
    *,
    video_id: str | None = None,
) -> Path:
    relative = Path(candidate.relative_txt)
    meta = NormalizedMeta.model_validate_json(candidate.meta_path.read_text(encoding="utf-8"))
    if meta.youtube_urls and card_kind == "episode":
        selected_video_id = video_id or _youtube_id(meta.youtube_urls[0])
        source = _dedicated_source_for_candidate(candidate, selected_video_id)
        if source is not None and Path(str(source["text_path"])).resolve().is_relative_to(
            paths.workspace.resolve()
        ):
            return _episode_card_path(source)
        channel = re.sub(
            r"[^\w.-]+",
            "_",
            str(meta.channel_id or meta.channel_name or relative.parent.name),
        )
        message_id = str(meta.message_id or relative.stem)
        return paths.enriched_dir / channel / (
            f"{message_id}.youtube.{selected_video_id}.enriched.json"
        )
    return paths.enriched_dir / relative.parent / f"{relative.stem}.enriched.json"


def _dedicated_source_for_candidate(
    candidate: PilotCandidate,
    video_id: str,
) -> dict[str, Any] | None:
    try:
        meta = NormalizedMeta.model_validate_json(candidate.meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    expected_message_id = str(meta.message_id or candidate.txt_path.stem)
    for source in _iter_dedicated_sources(None):
        if (
            str(source.get("video_id") or "") == video_id
            and str(source.get("message_id") or "") == expected_message_id
        ):
            return source
    return None


def _has_youtube_url(candidate: PilotCandidate) -> bool:
    try:
        meta = NormalizedMeta.model_validate_json(candidate.meta_path.read_text(encoding="utf-8"))
        return bool(meta.youtube_urls)
    except (OSError, UnicodeError, ValueError):
        return False


def _expected_card_kinds(candidate: PilotCandidate) -> tuple[str, ...]:
    if not _has_youtube_url(candidate):
        return ("telegram",)
    try:
        meta = NormalizedMeta.model_validate_json(candidate.meta_path.read_text(encoding="utf-8"))
        text = candidate.txt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        return ("episode",)
    if meta.has_body_text is True:
        return ("telegram", "episode")
    if meta.has_body_text is False or _looks_like_legacy_youtube_document(text):
        return ("episode",)
    return ("telegram", "episode")


def _youtube_id(url: str) -> str:
    match = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]+)", str(url))
    return match.group(1) if match else "unknown"


def _load_curated_golden_manifest(
    manifest_path: Path | None,
    selected: Sequence[PilotCandidate],
    paths: PilotPaths,
) -> dict[str, Any] | None:
    if manifest_path is None:
        return None
    resolved_manifest = _resolved_path(manifest_path)
    try:
        raw = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read curated golden manifest: {_safe_error(exc)}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("queries"), list):
        raise ValueError("Curated golden manifest must be an object with a queries list")

    selected_by_file = {item.relative_txt.replace("\\", "/"): item for item in selected}
    selected_ids = {
        _expected_card_identity(item, paths)[0]
        for item in selected
    }
    normalized_queries: list[dict[str, Any]] = []
    for index, entry in enumerate(raw["queries"], start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Golden query #{index} must be an object")
        query = " ".join(str(entry.get("query") or "").split())
        if not query:
            raise ValueError(f"Golden query #{index} has an empty query")

        source_ids = _string_list(entry.get("must_find_source_ids"), f"query #{index}")
        relative_files = _string_list(
            entry.get("must_find_normalized_files"),
            f"query #{index}",
        )
        for relative_file in relative_files:
            normalized_file = _safe_manifest_relative_path(relative_file)
            candidate = selected_by_file.get(normalized_file)
            if candidate is None:
                raise ValueError(
                    f"Golden query #{index} references a normalized file outside the selected pilot: "
                    f"{relative_file}"
                )
            source_ids.append(_expected_card_identity(candidate, paths)[0])

        source_ids = list(dict.fromkeys(source_ids))
        if not source_ids:
            raise ValueError(
                f"Golden query #{index} needs must_find_source_ids or "
                "must_find_normalized_files"
            )
        unknown_ids = [source_id for source_id in source_ids if source_id not in selected_ids]
        if unknown_ids:
            raise ValueError(
                f"Golden query #{index} references source IDs outside the selected pilot: "
                f"{', '.join(unknown_ids)}"
            )
        normalized_queries.append(
            {
                "query": query,
                "must_find_source_ids": source_ids,
            }
        )

    if not normalized_queries:
        raise ValueError("Curated golden manifest must contain at least one query")
    return {
        "manifest_path": str(resolved_manifest),
        "queries": normalized_queries,
    }


def _string_list(value: Any, context: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"Golden {context} expectation fields must be lists of strings")
    return [item.strip() for item in value if item.strip()]


def _safe_manifest_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"Golden normalized file must be a safe relative path: {value}")
    return path.as_posix()


def _find_legacy_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in LEGACY_FIELDS:
                found.add(key)
            found.update(_find_legacy_keys(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_find_legacy_keys(nested))
    return found


def _semantic_blob(card: dict[str, Any]) -> str:
    fields = (
        "summary",
        "key_points",
        "entities",
        "topics",
        "theses",
        "quotes",
        "events",
        "search_phrases",
        "graph_text",
        "search_text",
    )
    return json.dumps(
        {field: card.get(field) for field in fields},
        ensure_ascii=False,
        sort_keys=True,
    ).casefold()


def _run_recall_checks(
    card_results: Sequence[dict[str, Any]],
    paths: PilotPaths,
    *,
    curated_golden: dict[str, Any] | None = None,
    golden_top_k: int = DEFAULT_GOLDEN_TOP_K,
) -> dict[str, Any]:
    self_checks: list[dict[str, Any]] = []
    for card_result in card_results:
        if card_result.get("status") != "valid":
            continue
        card_files = card_result.get("card_files") or [card_result["card_file"]]
        for card_file in card_files:
            card_path = paths.pilot_dir / card_file
            try:
                card = EnrichedCardV2.model_validate_json(card_path.read_text(encoding="utf-8"))
                query = _choose_recall_query(card)
                returned = (
                    _union_source_ids(query, paths, top_k=SELF_RECALL_TOP_K)
                    if query
                    else []
                )
                must_find = card.provenance.source_id
                rank = returned.index(must_find) + 1 if must_find in returned else None
                self_checks.append(
                    {
                        "check_type": "self_recall",
                        "card_file": card_file,
                        "query": query or "",
                        "must_find_source_id": must_find,
                        "returned_source_ids": returned,
                        "rank": rank,
                        "passed": bool(query) and rank is not None and rank <= SELF_RECALL_TOP_K,
                    }
                )
            except Exception as exc:
                self_checks.append(
                    {
                        "check_type": "self_recall",
                        "card_file": card_file,
                        "query": "",
                        "must_find_source_id": card_result.get("source_id", ""),
                        "returned_source_ids": [],
                        "passed": False,
                        "error": _safe_error(exc),
                    }
                )

    golden_checks: list[dict[str, Any]] = []
    if curated_golden is not None:
        for expectation in curated_golden["queries"]:
            query = expectation["query"]
            must_find = expectation["must_find_source_ids"]
            returned = _union_source_ids(query, paths, top_k=golden_top_k)
            ranks = {
                source_id: (returned.index(source_id) + 1 if source_id in returned else None)
                for source_id in must_find
            }
            golden_checks.append(
                {
                    "check_type": "curated_golden",
                    "query": query,
                    "must_find_source_ids": must_find,
                    "returned_source_ids": returned,
                    "ranks": ranks,
                    "passed": all(
                        rank is not None and rank <= golden_top_k
                        for rank in ranks.values()
                    ),
                }
            )

    return {
        "self_recall": {
            "configured": True,
            "top_k": SELF_RECALL_TOP_K,
            "checks": self_checks,
            "passed": bool(self_checks) and all(item["passed"] for item in self_checks),
        },
        "curated_golden": {
            "configured": curated_golden is not None,
            "manifest_path": (curated_golden or {}).get("manifest_path", ""),
            "top_k": golden_top_k,
            "checks": golden_checks,
            "passed": (
                all(item["passed"] for item in golden_checks)
                if curated_golden is not None
                else None
            ),
        },
    }


def _validate_long_youtube_outputs(
    selected: Sequence[PilotCandidate],
    paths: PilotPaths,
    report: dict[str, Any],
) -> dict[str, Any]:
    long_candidates = [item for item in selected if "youtube_long" in item.traits]
    if not long_candidates:
        return {
            "required": False,
            "passed": True,
            "reason": "selected YouTube candidate did not cross the long-form threshold",
        }

    candidate = long_candidates[0]
    video_ids = candidate.youtube_long_video_ids
    checks: dict[str, bool] = {}
    violations = report.setdefault("youtube_long_form_violations", [])
    for video_id in video_ids:
        label = str(video_id)
        try:
            source = _dedicated_source_for_candidate(candidate, label)
            if source is None:
                raise ValueError("Dedicated YouTube source was not found in the isolated workspace")
            card_path = _expected_card_path_for_kind(
                candidate,
                paths,
                "episode",
                video_id=label,
            )
            specs = build_segment_specs(
                str(source.get("transcript_text") or ""),
                cues=source.get("cues") or [],
                chapters=source.get("chapters") or [],
            )
            fingerprint = _source_fingerprint(source, str(source.get("transcript_text") or ""))
            validation = _validate_generation(card_path, source, fingerprint, specs)
            checks[f"{label}_generation_valid"] = validation.valid and len(validation.segment_ids) >= 2
            checks[f"{label}_segments_indexed"] = (
                list_youtube_segment_ids(_episode_source_id(source), paths.card_fts_db)
                == set(validation.segment_ids)
            )
            violations.extend(f"{label}:{violation}" for violation in validation.violations)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, ValidationError) as exc:
            checks[f"{label}_generation_valid"] = False
            checks[f"{label}_segments_indexed"] = False
            violations.append(f"{label}:{type(exc).__name__}: {_safe_error(exc)}")

    return {
        "required": True,
        "passed": all(checks.values()),
        "candidate": candidate.relative_txt,
        "video_ids": list(video_ids),
        "checks": checks,
    }


def _choose_recall_query(card: EnrichedCardV2) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    for phrase in card.search_phrases:
        candidates.append((0, 0, phrase.text))
    for topic in card.topics:
        salience = 0 if topic.salience == "primary" else 1
        candidates.append((1, salience, topic.label))
    for category in (
        "countries",
        "organizations",
        "people",
        "programs_projects",
        "locations",
        "military_units",
        "equipment",
        "weapons",
        "media_sources",
        "other",
    ):
        for entity in getattr(card.entities, category):
            salience = 0 if entity.salience == "primary" else 1
            candidates.append((2, salience, entity.text))

    concrete = [
        (field_priority, salience, " ".join(text.strip().split()))
        for field_priority, salience, text in candidates
        if _is_concrete_query(text)
    ]
    if not concrete:
        return None
    concrete.sort(
        key=lambda item: (
            0 if len(item[2].split()) > 1 else 1,
            item[0],
            item[1],
            -len(item[2]),
            item[2].casefold(),
        )
    )
    return concrete[0][2]


def _is_concrete_query(value: str) -> bool:
    normalized = " ".join(str(value or "").casefold().split())
    words = normalized.split()
    return (
        len(normalized) >= 3
        and normalized not in GENERIC_QUERIES
        and not (len(words) == 1 and normalized in GENERIC_SINGLE_QUERIES)
        and any(character.isalpha() for character in normalized)
    )


def _union_source_ids(query: str, paths: PilotPaths, top_k: int = SELF_RECALL_TOP_K) -> list[str]:
    source_ids: list[str] = []
    seen: set[str] = set()
    for match in search_card_index(query, top_k=top_k, db_path=paths.card_fts_db):
        if match.source_id and match.source_id not in seen:
            seen.add(match.source_id)
            source_ids.append(match.source_id)
    for match in shadow_search.search(query, top_k=top_k):
        try:
            card_path = Path(match.card_path or "").resolve()
            if not card_path.is_relative_to(paths.enriched_dir.resolve()):
                continue
            card = EnrichedCardV2.model_validate_json(card_path.read_text(encoding="utf-8"))
            source_id = card.provenance.source_id
        except Exception:
            continue
        if source_id not in seen:
            seen.add(source_id)
            source_ids.append(source_id)
        if len(source_ids) >= top_k:
            break
    return source_ids[:top_k]


def _report_passes(report: dict[str, Any], selected: Sequence[PilotCandidate]) -> bool:
    stats = report.get("enrichment_stats") or {}
    cards = report.get("cards") or []
    recall = report.get("recall") or {}
    self_recall = recall.get("self_recall") or {}
    curated = recall.get("curated_golden") or {}
    long_form = report.get("youtube_long_form") or {}
    media = report.get("media") or {}
    long_form_passes = not long_form.get("required") or long_form.get("passed") is True
    curated_passes = not curated.get("configured") or curated.get("passed") is True
    expected_recall_checks = sum(
        len(item.get("card_files") or [item.get("card_file")])
        for item in cards
        if item.get("status") == "valid"
    )
    return (
        not report.get("errors")
        and stats.get("failed", 0) == 0
        and stats.get("youtube_failed", 0) == 0
        and stats.get("youtube_partial", 0) == 0
        and stats.get("partial", 0) == 0
        and stats.get("skipped_review", 0) == 0
        and len(cards) == len(selected)
        and all(item.get("status") == "valid" for item in cards)
        and len(self_recall.get("checks") or []) == expected_recall_checks
        and self_recall.get("passed") is True
        and curated_passes
        and long_form_passes
        and media.get("passed") is True
    )


def _audit_media(
    selected: Sequence[PilotCandidate],
    paths: PilotPaths,
) -> dict[str, Any]:
    """Fail the pilot when downloaded native media silently becomes text-only."""
    issues: list[str] = []
    native_media_items = 0
    images_checked = 0
    youtube_sources_checked = 0
    for candidate in selected:
        meta_path = paths.normalized_dir / candidate.relative_meta
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            issues.append(f"{candidate.relative_meta}:metadata_unreadable:{type(exc).__name__}")
            continue
        for media in meta.get("media") or []:
            if not isinstance(media, dict):
                continue
            media_type = str(media.get("media_type") or "").strip().lower()
            status = str(media.get("download_status") or "").strip().lower()
            message_id = str(media.get("message_id") or "unknown")
            if status != "downloaded":
                continue
            if media_type in {"video", "audio", "voice"}:
                native_media_items += 1
                transcription = str(media.get("transcription_status") or "").strip().lower()
                if transcription not in {"transcribed", "no_speech"}:
                    issues.append(
                        f"{candidate.relative_txt}:{media_type}:{message_id}:"
                        f"transcription_{transcription or 'missing'}"
                    )
                if media_type == "video":
                    visual = str(media.get("visual_status") or "").strip().lower()
                    if visual != "success":
                        issues.append(
                            f"{candidate.relative_txt}:video:{message_id}:"
                            f"visual_{visual or 'missing'}"
                        )
            elif media_type == "image":
                images_checked += 1
                visual = str(media.get("visual_status") or "").strip().lower()
                if visual != "success":
                    issues.append(
                        f"{candidate.relative_txt}:image:{message_id}:"
                        f"visual_{visual or 'missing'}"
                    )
        for source in meta.get("youtube_sources") or []:
            if not isinstance(source, dict):
                continue
            youtube_sources_checked += 1
            if not source.get("has_transcript", True):
                issues.append(f"{candidate.relative_txt}:youtube_transcript_unavailable")
    return {
        "passed": not issues,
        "native_media_items": native_media_items,
        "images_checked": images_checked,
        "youtube_sources_checked": youtube_sources_checked,
        "issues": issues,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(nested) for nested in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _relative_or_string(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _safe_error(exc: Exception) -> str:
    errors_method = getattr(exc, "errors", None)
    if callable(errors_method):
        try:
            errors = errors_method(include_url=False, include_context=False, include_input=False)
            compact = [
                {
                    "loc": ".".join(str(part) for part in error.get("loc", ())),
                    "type": str(error.get("type") or "validation_error"),
                }
                for error in errors[:20]
            ]
            return f"{type(exc).__name__}: {json.dumps(compact, ensure_ascii=False)}"
        except (AttributeError, TypeError, ValueError):
            pass
    message = str(exc).replace("\n", " ").replace("\r", " ")
    message = re.sub(r"(?i)(api[_ -]?key|authorization|bearer)\s*[:=]?\s*\S+", r"\1=[REDACTED]", message)
    return f"{type(exc).__name__}: {message[:500]}"


def _configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError, ValueError):
                pass


def _print_selection(
    selected: Sequence[PilotCandidate],
    run_id: str,
    paths: PilotPaths,
    *,
    dry_run: bool,
) -> None:
    print(f"Enriched v2 pilot {'dry-run' if dry_run else 'LIVE RUN'}")
    print(f"Run ID: {run_id}")
    print(f"Pilot directory: {paths.pilot_dir}")
    print(f"Selected posts: {len(selected)}")
    for index, item in enumerate(selected, start=1):
        traits = ",".join(item.traits) or "base"
        print(
            f"{index:02d}. {item.relative_txt} | {item.content_type} | "
            f"body={item.body_char_count} | {traits} | nominal_model_calls={item.estimated_llm_calls}"
        )
    nominal_calls = sum(item.estimated_llm_calls for item in selected)
    max_model_calls = sum(item.estimated_llm_calls_with_repair for item in selected)
    max_http_requests = 2 * max_model_calls
    print(f"Nominal model calls: {nominal_calls}")
    print(
        "Theoretical max model calls with one repair per card: "
        f"{max_model_calls}"
    )
    print(
        "Theoretical max HTTP requests including one HTTP 400 fallback per model call: "
        f"{max_http_requests}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
