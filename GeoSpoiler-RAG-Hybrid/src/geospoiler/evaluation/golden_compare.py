import argparse
import json
from pathlib import Path
from typing import Any

FOCUS_TERMS = (
    "deepfake",
    "fake",
    "дипфейк",
    "фейк",
    "источник",
    "ссылк",
    "откуда",
    "тезис",
)


def load_summary(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def compare_summaries(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    baseline_cases = _cases_by_question(baseline)
    candidate_cases = _cases_by_question(candidate)
    questions = list(baseline_cases)
    questions.extend(question for question in candidate_cases if question not in baseline_cases)

    cases = []
    for index, question in enumerate(questions, 1):
        before = baseline_cases.get(question)
        after = candidate_cases.get(question)
        before_score = _case_score(before)
        after_score = _case_score(after)
        delta = None if before_score is None or after_score is None else after_score - before_score
        before_pass = _case_pass(before)
        after_pass = _case_pass(after)
        regression = bool(
            after is None
            or (before_pass is True and after_pass is False)
            or (delta is not None and delta < 0)
        )
        improvement = bool(
            before is None
            or (before_pass is False and after_pass is True)
            or (delta is not None and delta > 0)
        )
        focus = _is_focus_question(question)

        cases.append(
            {
                "index": index,
                "question": question,
                "baseline_score": before_score,
                "candidate_score": after_score,
                "delta": delta,
                "baseline_pass": before_pass,
                "candidate_pass": after_pass,
                "baseline_source_ok": _case_source_ok(before),
                "candidate_source_ok": _case_source_ok(after),
                "baseline_source_any_ok": _case_source_any_ok(before),
                "candidate_source_any_ok": _case_source_any_ok(after),
                "regression": regression,
                "improvement": improvement,
                "focus": focus,
            }
        )

    regressions = [case for case in cases if case["regression"]]
    improvements = [case for case in cases if case["improvement"]]
    focus_cases = [case for case in cases if case["focus"]]
    focus_regressions = [case for case in focus_cases if case["regression"]]

    return {
        "baseline": _summary_metadata(baseline),
        "candidate": _summary_metadata(candidate),
        "average_delta": _average_delta(cases),
        "total_cases": len(cases),
        "regressions": len(regressions),
        "improvements": len(improvements),
        "focus_cases": len(focus_cases),
        "focus_regressions": len(focus_regressions),
        "cases": cases,
    }


def write_markdown_report(comparison: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Golden Set Comparison",
        "",
        "## Summary",
        "",
        f"- Baseline: {_run_label(comparison['baseline'])}",
        f"- Candidate: {_run_label(comparison['candidate'])}",
        f"- Average score delta: {comparison['average_delta']:+.1f}",
        f"- Regressions: {comparison['regressions']} / {comparison['total_cases']}",
        f"- Improvements: {comparison['improvements']} / {comparison['total_cases']}",
        f"- Focus regressions: {comparison['focus_regressions']} / {comparison['focus_cases']}",
        "",
        "## Per Case",
        "",
        "| # | Focus | Base | New | Delta | Base pass | New pass | Source | Question |",
        "|---:|:---:|---:|---:|---:|:---:|:---:|:---:|---|",
    ]

    for case in comparison["cases"]:
        source_status = _source_status(case)
        lines.append(
            "| "
            f"{case['index']} | "
            f"{_yes_no(case['focus'])} | "
            f"{_score_text(case['baseline_score'])} | "
            f"{_score_text(case['candidate_score'])} | "
            f"{_delta_text(case['delta'])} | "
            f"{_yes_no(case['baseline_pass'])} | "
            f"{_yes_no(case['candidate_pass'])} | "
            f"{source_status} | "
            f"{_escape_table(str(case['question']))} |"
        )

    regressions = [case for case in comparison["cases"] if case["regression"]]
    if regressions:
        lines.extend(["", "## Regressions", ""])
        for case in regressions:
            lines.append(
                f"- Q{case['index']}: {_delta_text(case['delta'])}, "
                f"pass {_yes_no(case['baseline_pass'])} -> {_yes_no(case['candidate_pass'])}; "
                f"{case['question']}"
            )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cases_by_question(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cases = {}
    for case in summary.get("cases", []):
        question = str(case.get("question", ""))
        if question:
            cases[question] = case
    return cases


def _summary_metadata(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "checked_at": summary.get("checked_at", ""),
        "query_model": summary.get("query_model", ""),
        "query_base_url": summary.get("query_base_url", ""),
        "mode": summary.get("mode", ""),
        "passed": summary.get("passed"),
        "total": summary.get("total"),
        "average_score": summary.get("average_score"),
        "config_flags": summary.get("config_flags", {}),
    }


def _case_score(case: dict[str, Any] | None) -> int | None:
    if not case:
        return None
    score = case.get("score")
    return int(score) if isinstance(score, int | float) else None


def _case_pass(case: dict[str, Any] | None) -> bool | None:
    if not case:
        return None
    value = case.get("pass")
    return bool(value) if isinstance(value, bool) else None


def _case_source_ok(case: dict[str, Any] | None) -> bool | None:
    if not case:
        return None
    value = case.get("source_ok")
    return bool(value) if isinstance(value, bool) else None


def _case_source_any_ok(case: dict[str, Any] | None) -> bool | None:
    if not case:
        return None
    value = case.get("source_any_ok")
    return bool(value) if isinstance(value, bool) else None


def _is_focus_question(question: str) -> bool:
    text = question.casefold()
    return any(term in text for term in FOCUS_TERMS)


def _average_delta(cases: list[dict[str, Any]]) -> float:
    deltas = [case["delta"] for case in cases if case["delta"] is not None]
    if not deltas:
        return 0.0
    return round(sum(deltas) / len(deltas), 1)


def _run_label(metadata: dict[str, Any]) -> str:
    return (
        f"{metadata.get('checked_at') or 'unknown time'}, "
        f"{metadata.get('passed')}/{metadata.get('total')} passed, "
        f"avg={metadata.get('average_score')}, "
        f"model={metadata.get('query_model')}, "
        f"mode={metadata.get('mode')}"
    )


def _source_status(case: dict[str, Any]) -> str:
    before = _source_pair(case["baseline_source_ok"], case["baseline_source_any_ok"])
    after = _source_pair(case["candidate_source_ok"], case["candidate_source_any_ok"])
    return f"{before}->{after}"


def _source_pair(source_ok: bool | None, source_any_ok: bool | None) -> str:
    return f"{_yes_no(source_ok)}/{_yes_no(source_any_ok)}"


def _score_text(score: int | None) -> str:
    return "n/a" if score is None else str(score)


def _delta_text(delta: int | None) -> str:
    return "n/a" if delta is None else f"{delta:+d}"


def _yes_no(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "Y" if value else "N"


def _escape_table(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two golden-set score JSON files.")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts/golden_set_comparison.md"))
    args = parser.parse_args()

    comparison = compare_summaries(load_summary(args.baseline), load_summary(args.candidate))
    write_markdown_report(comparison, args.output)
    print(
        "Golden comparison: "
        f"avg_delta={comparison['average_delta']:+.1f}, "
        f"regressions={comparison['regressions']}, "
        f"focus_regressions={comparison['focus_regressions']}"
    )


if __name__ == "__main__":
    main()
