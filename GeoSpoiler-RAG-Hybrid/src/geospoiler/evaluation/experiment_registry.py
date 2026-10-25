"""Lightweight experiment registry for golden/probe artifact summaries."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geospoiler import config


@dataclass(frozen=True)
class ExperimentRecord:
    run_id: str
    kind: str
    checked_at: str
    model: str
    mode: str
    passed: int
    total: int
    average_score: float
    average_duration_seconds: float | None
    reranker_enabled: bool | None
    hybrid_synth_enabled: bool | None
    scores_path: str
    results_path: str

    @property
    def pass_rate(self) -> float:
        return round((self.passed / self.total) * 100, 1) if self.total else 0.0


@dataclass(frozen=True)
class ExperimentRegistry:
    generated_at: str
    records: list[ExperimentRecord]
    manifest_path: Path
    report_path: Path


def collect_experiment_records(artifacts_dir: Path = config.PROJECT_ROOT / "artifacts") -> list[ExperimentRecord]:
    """Collect score summaries from artifacts/*scores.json."""
    records: list[ExperimentRecord] = []
    if not artifacts_dir.exists():
        return records

    for path in sorted(artifacts_dir.glob("*scores.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or "total" not in data:
            continue
        records.append(_record_from_summary(path, data))

    records.sort(key=lambda item: (item.checked_at or "", item.run_id))
    return records


def write_experiment_registry(
    artifacts_dir: Path = config.PROJECT_ROOT / "artifacts",
    manifest_path: Path | None = None,
    report_path: Path | None = None,
) -> ExperimentRegistry:
    records = collect_experiment_records(artifacts_dir)
    manifest_path = manifest_path or artifacts_dir / "experiment_registry.json"
    report_path = report_path or artifacts_dir / "experiment_registry.md"
    generated_at = _utc_now()

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": generated_at,
        "records": [
            {
                **asdict(record),
                "pass_rate": record.pass_rate,
            }
            for record in records
        ],
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown_report(records, report_path, generated_at)

    return ExperimentRegistry(
        generated_at=generated_at,
        records=records,
        manifest_path=manifest_path,
        report_path=report_path,
    )


def _record_from_summary(path: Path, data: dict[str, Any]) -> ExperimentRecord:
    flags = data.get("config_flags") if isinstance(data.get("config_flags"), dict) else {}
    return ExperimentRecord(
        run_id=path.stem.removesuffix("_scores"),
        kind=_infer_kind(path.name),
        checked_at=_clean_str(data.get("checked_at")),
        model=_clean_str(data.get("query_model") or data.get("model")),
        mode=_clean_str(data.get("mode")),
        passed=_int_value(data.get("passed")),
        total=_int_value(data.get("total")),
        average_score=_float_value(data.get("average_score")),
        average_duration_seconds=_optional_float(data.get("average_duration_seconds")),
        reranker_enabled=_optional_bool(flags.get("RERANKER_ENABLED")),
        hybrid_synth_enabled=_optional_bool(flags.get("HYBRID_SYNTH_ENABLED")),
        scores_path=str(path),
        results_path=str(_paired_results_path(path)),
    )


def _write_markdown_report(records: list[ExperimentRecord], path: Path, generated_at: str) -> None:
    lines = [
        "# Experiment Registry",
        "",
        f"- generated_at: {generated_at}",
        f"- records: {len(records)}",
        "",
        "| Checked At | Kind | Model | Mode | Passed | Avg | Rerank | Synth | Scores |",
        "|---|---|---|---|---:|---:|:---:|:---:|---|",
    ]
    for record in records:
        lines.append(
            "| "
            f"{_cell(record.checked_at)} | "
            f"{_cell(record.kind)} | "
            f"{_cell(record.model)} | "
            f"{_cell(record.mode)} | "
            f"{record.passed}/{record.total} | "
            f"{record.average_score:g} | "
            f"{_flag(record.reranker_enabled)} | "
            f"{_flag(record.hybrid_synth_enabled)} | "
            f"`{Path(record.scores_path).name}` |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _infer_kind(filename: str) -> str:
    lower = filename.lower()
    if "llm_probe" in lower:
        return "focused_probe"
    if "golden" in lower:
        return "golden"
    if "baseline" in lower:
        return "baseline"
    return "scores"


def _paired_results_path(scores_path: Path) -> Path:
    name = scores_path.name
    if name.endswith("_scores.json"):
        return scores_path.with_name(name.removesuffix("_scores.json") + "_results.md")
    return scores_path.with_suffix(".md")


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float_value(value: Any) -> float:
    return _optional_float(value) or 0.0


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _flag(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "Y" if value else "N"


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|")


def _clean_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
