"""Re-score a saved model bakeoff run without calling model APIs."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from geospoiler.evaluation.model_bakeoff.aggregate_report import write_reports
from geospoiler.evaluation.model_bakeoff.config_loader import load_model_registry
from geospoiler.evaluation.model_bakeoff.run_bakeoff import (
    DEFAULT_MODELS_PATH,
    _score_record,
    _write_score_csvs,
    load_suite,
)


def rescore_run(
    *,
    source_run_dir: Path,
    suite_paths: list[Path],
    output_run_dir: Path | None = None,
    models_path: Path = DEFAULT_MODELS_PATH,
) -> Path:
    """Re-score saved model outputs using current suite expectations."""
    target_run_dir = output_run_dir or source_run_dir
    if output_run_dir and output_run_dir.resolve() != source_run_dir.resolve():
        _copy_run_shell(target_run_dir, source_run_dir)

    cases = _case_map(suite_paths)
    models = {model.id: model for model in load_model_registry(models_path)}
    score_records: list[dict[str, Any]] = []
    for output_path in sorted((target_run_dir / "model_outputs").glob("*/*.json")):
        record = json.loads(output_path.read_text(encoding="utf-8"))
        case = cases.get(record.get("case_id"))
        model = models.get(record.get("model_id"))
        if case is None or model is None:
            continue
        score_records.append(_score_record(record, model, case))

    scores_dir = target_run_dir / "scores"
    scores_dir.mkdir(parents=True, exist_ok=True)
    _write_score_csvs(scores_dir, score_records)
    write_reports(target_run_dir, score_records)
    return target_run_dir


def _copy_run_shell(target: Path, source: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def _case_map(suite_paths: list[Path]) -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for suite_path in suite_paths:
        for case in load_suite(suite_path):
            cases[str(case["id"])] = case
    return cases


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Re-score a saved model bakeoff run.")
    parser.add_argument("--source-run-dir", type=Path, required=True)
    parser.add_argument("--output-run-dir", type=Path)
    parser.add_argument("--suite", action="append", type=Path, required=True)
    parser.add_argument("--models", type=Path, default=DEFAULT_MODELS_PATH)
    args = parser.parse_args(argv)
    run_dir = rescore_run(
        source_run_dir=args.source_run_dir,
        output_run_dir=args.output_run_dir,
        suite_paths=args.suite,
        models_path=args.models,
    )
    print(f"Re-scored model bakeoff artifacts: {run_dir}")


if __name__ == "__main__":
    main()
