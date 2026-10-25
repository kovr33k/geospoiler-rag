"""Build an auditable blind comparison of two completed Late-Fusion runs."""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path
from typing import Any

from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.operations import late_fusion_ab  # noqa: E402


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected object in {path}")
    return payload


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _variant_block(label: str, variant: dict[str, Any]) -> list[str]:
    return [
        f"### {label}",
        "",
        str(variant.get("answer") or ""),
        "",
        *late_fusion_ab._render_blind_references(variant),
        "",
    ]


def prepare(baseline_dir: Path, candidate_dir: Path, output_dir: Path) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError("Comparison directory is not empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, dict[str, str]] = {}
    ratings = {
        "criteria": list(late_fusion_ab.CRITERIA),
        "cases": [
            {"id": case["id"], **{criterion: None for criterion in late_fusion_ab.CRITERIA}}
            for case in late_fusion_ab.FROZEN_CASES
        ],
    }
    lines = [
        "# Blind reranker OFF/ON comparison",
        "",
        "Compare A and B without opening mapping.json. Rate completeness, specificity, relevance, no_unsupported, and citations.",
        "",
    ]
    metrics: list[dict[str, Any]] = []
    for case in late_fusion_ab.FROZEN_CASES:
        case_id = case["id"]
        baseline = _read(baseline_dir / "cases" / f"{case_id}.json")
        candidate = _read(candidate_dir / "cases" / f"{case_id}.json")
        if baseline.get("question") != candidate.get("question") or baseline.get("question") != case["question"]:
            raise RuntimeError(f"{case_id}: question mismatch")
        off = baseline.get("late_fusion")
        on = candidate.get("late_fusion")
        if not isinstance(off, dict) or not isinstance(on, dict):
            raise RuntimeError(f"{case_id}: missing Late-Fusion variant")
        stats = on.get("reranker") if isinstance(on.get("reranker"), dict) else {}
        if not (
            int(stats.get("attempted") or 0) > 0
            and stats.get("attempted") == stats.get("succeeded")
            and stats.get("failed") == 0
        ):
            raise RuntimeError(f"{case_id}: candidate reranker was not fully successful")
        if secrets.randbits(1):
            pair = {"A": "off", "B": "on"}
        else:
            pair = {"A": "on", "B": "off"}
        mapping[case_id] = pair
        variants = {"off": off, "on": on}
        lines.extend([f"## {case_id}", "", f"Question: {case['question']}", ""])
        lines.extend(_variant_block("A", variants[pair["A"]]))
        lines.extend(_variant_block("B", variants[pair["B"]]))
        off_trace = off.get("late_fusion") if isinstance(off.get("late_fusion"), dict) else {}
        on_trace = on.get("late_fusion") if isinstance(on.get("late_fusion"), dict) else {}
        metrics.append(
            {
                "id": case_id,
                "off_lightrag_duration_ms": ((off_trace.get("channel_statuses") or {}).get("lightrag") or {}).get("duration_ms"),
                "on_lightrag_duration_ms": ((on_trace.get("channel_statuses") or {}).get("lightrag") or {}).get("duration_ms"),
                "off_fallback": bool(off.get("fallback")),
                "on_fallback": bool(on.get("fallback")),
                "off_reference_count": len(off.get("references") or []),
                "on_reference_count": len(on.get("references") or []),
                "on_reranker": stats,
            }
        )
    _write(output_dir / "mapping.json", mapping)
    _write(output_dir / "ratings.json", ratings)
    _write(output_dir / "metrics.json", {"cases": metrics})
    (output_dir / "BLIND_REVIEW.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.baseline_dir, args.candidate_dir, args.output_dir)


if __name__ == "__main__":
    main()
