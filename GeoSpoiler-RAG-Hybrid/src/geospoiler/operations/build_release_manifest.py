"""Build the fail-closed accepted_manifest_v2 for the full enriched corpus."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from geospoiler import config
from geospoiler.contracts.models import EnrichedCardV2
from geospoiler.operations.release_identity import (
    SafeArgumentParser,
    atomic_write_release_json,
    config_identity_sha256,
    run_release_cli,
    safe_exception_token,
    safe_resolve_release_path,
)
from geospoiler.providers import llm_backend
from geospoiler.retrieval.lightrag.manifest_contract import validate_manifest_payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    return str(path.resolve().relative_to(config.PROJECT_ROOT.resolve()))


def _opaque_identifier(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="surrogatepass")).hexdigest()


def build(output: Path) -> dict:
    cards = sorted(config.ENRICHED_DIR.rglob("*.enriched.json"))
    entries: list[dict] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    errors: list[str] = []
    for card_path in cards:
        try:
            card = EnrichedCardV2.model_validate_json(card_path.read_text(encoding="utf-8"))
        except Exception as exc:
            diagnostic = safe_exception_token(
                "invalid_card",
                exc,
                component="build_release_manifest",
            )
            errors.append(f"invalid_card:{_opaque_identifier(card_path)}:{diagnostic}")
            continue
        source_id = card.provenance.source_id
        normalized_value = card.provenance.normalized_path
        normalized_path = config.PROJECT_ROOT / Path(normalized_value)
        if not normalized_path.is_file():
            errors.append(
                "normalized_missing:"
                f"{_opaque_identifier(card_path)}:{_opaque_identifier(normalized_value)}"
            )
            continue
        normalized_relative = _relative(normalized_path)
        card_relative = _relative(card_path)
        if source_id in seen_ids:
            errors.append(f"duplicate_source_id:{_opaque_identifier(source_id)}")
        if normalized_relative in seen_paths:
            errors.append(
                f"duplicate_normalized_path:{_opaque_identifier(normalized_relative)}"
            )
        seen_ids.add(source_id)
        seen_paths.add(normalized_relative)
        blocked = sorted({"extraction_unstable", "partial_segment_failure"}.intersection(card.quality_flags))
        if blocked:
            errors.append(
                f"blocked_quality_flags:{_opaque_identifier(source_id)}:{','.join(blocked)}"
            )
        entries.append(
            {
                "accepted": not blocked,
                "document_kind": "youtube_episode" if ".youtube." in card_path.name else "telegram_card",
                "source_id": source_id,
                "normalized_path": normalized_relative,
                "card_path": card_relative,
                "normalized_sha256": _sha256(normalized_path),
                "enriched_sha256": _sha256(card_path),
                "graph_text_sha256": hashlib.sha256(card.graph_text.encode("utf-8")).hexdigest(),
                "quality_flags": list(card.quality_flags),
            }
        )
    entries.sort(key=lambda item: item["source_id"])
    expected = len(cards)
    if len(entries) != expected:
        errors.append(f"card_count_mismatch:{len(entries)}:{expected}")
    if len(entries) != len(seen_ids):
        errors.append("source_id_count_mismatch")
    if errors:
        raise RuntimeError("; ".join(errors))
    if not entries:
        raise RuntimeError("release manifest has no accepted sources")
    payload = {
        "schema_version": "accepted_manifest_v2",
        "run_id": output.stem,
        "generated_at": datetime.now(UTC).isoformat(),
        "pipeline_identity": {
            "llm_profile": config.LLM_PROFILE,
            "enrichment_model": llm_backend.active_model_for("enrichment"),
            "media_contract_version": "media-contract-v2",
            "late_fusion_enabled": config.LATE_FUSION_ENABLED,
            "reranker_enabled": config.RERANKER_ENABLED,
            "wiki_enabled": config.WIKI_ENABLED,
            "expected_cards": expected,
            "config_identity_sha256": config_identity_sha256(),
        },
        "sources": entries,
    }
    validation = validate_manifest_payload(payload)
    if validation.errors:
        raise RuntimeError("release manifest failed shared validation")
    atomic_write_release_json(output, payload, label="accepted_release_manifest")
    return {"cards": expected, "sources": len(entries), "output": str(output), "errors": 0}


def main() -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=config.PROJECT_ROOT / "artifacts" / "accepted_manifest_v2_release.json",
    )
    args = parser.parse_args()
    output = safe_resolve_release_path(args.output, label="release_manifest_output_path")
    print(json.dumps(build(output), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_release_cli(main, component="build_release_manifest"))
