"""Validation for enriched v2 cards."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from geospoiler import config
from geospoiler.contracts.models import EnrichedCardV2, LLMPayload
from geospoiler.ingestion.enricher.validator import non_russian_prose_reason, required_russian_violations


@dataclass(frozen=True)
class ContractIssue:
    severity: str
    code: str
    path: str
    message: str
    field: str = ""


@dataclass
class EnrichedValidationReport:
    cards_seen: int = 0
    cards_valid: int = 0
    cards_invalid: int = 0
    errors: list[ContractIssue] = field(default_factory=list)
    warnings: list[ContractIssue] = field(default_factory=list)
    report_path: Path | None = None

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)


def validate_enriched_card(data: dict[str, Any], path: Path | str = "") -> tuple[EnrichedCardV2 | None, list[ContractIssue]]:
    """Validate one enriched v2 card and return non-fatal contract issues."""
    path_text = str(path)
    issues: list[ContractIssue] = []
    try:
        card = EnrichedCardV2.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            field_path = ".".join(str(part) for part in err.get("loc", ()))
            issues.append(
                ContractIssue(
                    severity="error",
                    code="schema_error",
                    path=path_text,
                    field=field_path,
                    message=str(err.get("msg", "validation error")),
                )
            )
        return None, issues

    has_content = card.summary.strip() or card.key_points
    if not has_content:
        issues.append(
            ContractIssue(
                severity="warning",
                code="empty_evidence_summary",
                path=path_text,
                field="summary,key_points",
                message="Card has neither summary nor key_points.",
            )
        )

    payload = LLMPayload.model_validate(
        {
            field_name: getattr(card, field_name)
            for field_name in LLMPayload.model_fields
        }
    )
    for violation in required_russian_violations(payload):
        field_name = violation.split(":", 2)[1].strip().split(" ", 1)[0]
        issues.append(
            ContractIssue(
                severity="error",
                code="non_russian_semantic_field",
                path=path_text,
                field=field_name,
                message=violation,
            )
        )

    for index, quote in enumerate(card.quotes):
        quote_text = quote.text.strip()
        if (
            len(quote_text) >= 12
            and quote_text in card.graph_text
            and non_russian_prose_reason(quote_text) is not None
        ):
            issues.append(
                ContractIssue(
                    severity="error",
                    code="non_russian_quote_in_graph_text",
                    path=path_text,
                    field=f"quotes.{index}.text,graph_text",
                    message="Non-Russian verbatim quote leaked into graph_text.",
                )
            )

    return card, issues


def scan_enriched_cards(enriched_dir: Path = config.ENRICHED_DIR) -> EnrichedValidationReport:
    """Scan output/enriched without mutating cards or stopping on bad files."""
    report = EnrichedValidationReport()
    if not enriched_dir.exists():
        report.errors.append(
            ContractIssue(
                severity="error",
                code="missing_enriched_dir",
                path=str(enriched_dir),
                message="Enriched directory does not exist.",
            )
        )
        return report

    for path in sorted(enriched_dir.rglob("*.enriched.json")):
        report.cards_seen += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            report.cards_invalid += 1
            report.errors.append(
                ContractIssue(
                    severity="error",
                    code="invalid_json",
                    path=str(path),
                    message=str(exc),
                )
            )
            continue
        except OSError as exc:
            report.cards_invalid += 1
            report.errors.append(
                ContractIssue(
                    severity="error",
                    code="read_error",
                    path=str(path),
                    message=str(exc),
                )
            )
            continue

        if not isinstance(data, dict):
            report.cards_invalid += 1
            report.errors.append(
                ContractIssue(
                    severity="error",
                    code="schema_error",
                    path=str(path),
                    message="Enriched card root must be a JSON object.",
                )
            )
            continue

        card, issues = validate_enriched_card(data, path)
        report.errors.extend(issue for issue in issues if issue.severity == "error")
        report.warnings.extend(issue for issue in issues if issue.severity != "error")
        if card is None or any(issue.severity == "error" for issue in issues):
            report.cards_invalid += 1
        else:
            report.cards_valid += 1

    return report


def write_enriched_validation_report(
    report: EnrichedValidationReport,
    output_path: Path | None = None,
) -> Path:
    """Write a compact Markdown validation report."""
    if output_path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        output_path = config.PROJECT_ROOT / "artifacts" / f"enriched_validation_{stamp}.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Enriched Card Validation Report",
        "",
        f"- generated_at: {datetime.now(UTC).replace(microsecond=0).isoformat()}",
        f"- cards_seen: {report.cards_seen}",
        f"- cards_valid: {report.cards_valid}",
        f"- cards_invalid: {report.cards_invalid}",
        f"- errors: {report.error_count}",
        f"- warnings: {report.warning_count}",
        "",
    ]

    if report.errors:
        lines.extend(["## Errors", ""])
        lines.extend(_issue_lines(report.errors))
        lines.append("")

    if report.warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(_issue_lines(report.warnings))
        lines.append("")

    if not report.errors and not report.warnings:
        lines.extend(["No contract issues found.", ""])

    output_path.write_text("\n".join(lines), encoding="utf-8")
    report.report_path = output_path
    return output_path


def _issue_lines(issues: list[ContractIssue], limit: int = 100) -> list[str]:
    rows = []
    for issue in issues[:limit]:
        field_str = f" `{issue.field}`" if issue.field else ""
        rows.append(f"- `{issue.code}`{field_str}: {issue.path} - {issue.message}")
    if len(issues) > limit:
        rows.append(f"- ... {len(issues) - limit} more issue(s) omitted")
    return rows
