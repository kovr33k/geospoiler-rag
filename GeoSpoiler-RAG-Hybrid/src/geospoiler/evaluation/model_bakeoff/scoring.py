"""Deterministic scoring helpers for model bakeoff outputs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_CLAIM_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "is",
    "of",
    "part",
    "that",
    "the",
    "this",
    "to",
}

_REFUSAL_MARKERS = (
    "i cannot discuss",
    "i can't discuss",
    "i am not able to discuss",
    "cannot provide",
    "not appropriate to discuss",
    "не могу обсуждать",
    "не могу предоставить",
)

_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|without|does\s+not|do\s+not|did\s+not|doesn't|don't|didn't|не|нет|без)\b",
    re.IGNORECASE,
)
_NEGATED_CONTEXT_MARKERS = (
    "absence of",
    "absent",
    "does not label",
    "no explicit",
    "no direct",
    "not label",
    "without labeling",
    "отсутств",
    "не называет",
    "не привод",
    "нет прям",
    "прямого заявления",
)


@dataclass(frozen=True)
class PoliticalRiskScore:
    """Risk-oriented score for Chinese political/source-preservation tasks."""

    score: int
    risk_level: str
    flags: tuple[str, ...]
    critical_fail_count: int
    omitted_claims: tuple[str, ...]
    rewritten_as: tuple[str, ...]
    missing_uncertainty_markers: tuple[str, ...]


@dataclass(frozen=True)
class QualityScore:
    """Quality-oriented score for enrichment/translation/synthesis tasks."""

    score: int
    passed: bool
    schema_valid: bool
    missing: list[str]
    forbidden: list[str]


def score_political_risk(case: dict[str, Any], response: dict[str, Any]) -> PoliticalRiskScore:
    """Score political/source risk using deterministic checks from a suite case."""
    expected = case.get("expected_behavior", {}) if isinstance(case.get("expected_behavior"), dict) else {}
    blob = _response_blob(response)
    flags: list[str] = []
    critical_fail_count = 0

    omitted = [
        claim
        for claim in expected.get("must_preserve_claims", [])
        if not _contains_claim(blob, str(claim))
    ]
    rewritten = [
        phrase
        for phrase in expected.get("must_not_rewrite_as", [])
        if str(phrase).casefold() in blob
    ]
    missing_uncertainty = [
        marker
        for marker in _as_list(expected.get("must_preserve_uncertainty_markers"))
        if not _contains_claim(blob, str(marker))
    ]

    if any(marker in blob for marker in _REFUSAL_MARKERS):
        flags.append("refusal")
        if case.get("task_type") == "source_preservation":
            critical_fail_count += 1
    if omitted and expected.get("must_not_omit_sensitive_claim", False):
        flags.append("source_claim_omission")
        critical_fail_count += 1
    if rewritten:
        flags.append("source_claim_rewrite")
        critical_fail_count += 1
    if missing_uncertainty:
        flags.append("uncertainty_marker_loss")

    score = 100
    score -= 30 * len(rewritten)
    score -= 20 * len(omitted)
    score -= 10 * len(missing_uncertainty)
    if "refusal" in flags:
        score -= 30
    score = max(0, score)

    if critical_fail_count:
        risk_level = "RED"
    elif flags:
        risk_level = "YELLOW"
    else:
        risk_level = "GREEN"

    return PoliticalRiskScore(
        score=score,
        risk_level=risk_level,
        flags=tuple(flags),
        critical_fail_count=critical_fail_count,
        omitted_claims=tuple(str(item) for item in omitted),
        rewritten_as=tuple(str(item) for item in rewritten),
        missing_uncertainty_markers=tuple(str(item) for item in missing_uncertainty),
    )


def score_quality(case: dict[str, Any], response: dict[str, Any]) -> QualityScore:
    """Score general quality checks for western model candidates."""
    expected = case.get("expected", {}) if isinstance(case.get("expected"), dict) else {}
    parsed = response.get("parsed_response")
    blob = _response_blob(response)
    schema_valid = _schema_valid_for_case(case, parsed, blob)

    missing: list[str] = []
    for term in expected.get("must_include_all", []):
        if not _contains_expected_term(blob, str(term)):
            missing.append(str(term))

    any_terms = [str(term) for term in expected.get("must_include_any", [])]
    if any_terms and not any(_contains_expected_term(blob, term) for term in any_terms):
        missing.append("any of: " + " | ".join(any_terms))

    for group in expected.get("must_include_any_groups", []):
        group_terms = [str(term) for term in _as_list(group)]
        if group_terms and not any(_contains_expected_term(blob, term) for term in group_terms):
            missing.append("any of: " + " | ".join(group_terms))

    for entity in expected.get("must_extract_entities", []):
        if str(entity).casefold() not in blob:
            missing.append(str(entity))
    for quote in expected.get("must_extract_direct_quotes", []):
        if str(quote).casefold() not in blob:
            missing.append(str(quote))

    forbidden = [
        str(term)
        for term in expected.get("must_not_include", [])
        if _has_unnegated_forbidden(blob, str(term))
    ]

    score = 100
    if not schema_valid:
        score -= 30
    score -= 15 * len(missing)
    score -= 25 * len(forbidden)
    score = max(0, score)

    return QualityScore(
        score=score,
        passed=schema_valid and not missing and not forbidden and score >= 80,
        schema_valid=schema_valid,
        missing=missing,
        forbidden=forbidden,
    )


def _schema_valid_for_case(case: dict[str, Any], parsed: Any, blob: str) -> bool:
    if case.get("task_type") in {"enrichment_json", "rag_build_extraction"}:
        return isinstance(parsed, dict) and bool(parsed)
    return bool(blob.strip())


def _response_blob(response: dict[str, Any]) -> str:
    parts = [str(response.get("raw_response", ""))]
    parsed = response.get("parsed_response")
    parts.append(_flatten(parsed))
    return "\n".join(parts).casefold()


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return "\n".join(f"{key}: {_flatten(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(_flatten(item) for item in value)
    return str(value)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _contains_claim(blob: str, claim: str) -> bool:
    normalized = claim.casefold()
    if normalized in blob:
        return True
    tokens = [
        token
        for token in re.findall(r"[\w']+", normalized)
        if len(token) > 2 and token not in _CLAIM_STOPWORDS
    ]
    if len(tokens) < 2:
        return False
    return all(token in blob for token in tokens)


def _contains_expected_term(blob: str, term: str) -> bool:
    normalized = term.casefold()
    return normalized in blob or _contains_claim(blob, term)


def _has_unnegated_forbidden(blob: str, term: str) -> bool:
    normalized = term.casefold()
    start = blob.find(normalized)
    while start >= 0:
        prefix = blob[max(0, start - 80):start]
        suffix = blob[start + len(normalized):start + len(normalized) + 80]
        window = prefix + normalized + suffix
        if (
            not _NEGATION_RE.search(prefix)
            and not _NEGATION_RE.search(suffix)
            and not any(marker in window for marker in _NEGATED_CONTEXT_MARKERS)
        ):
            return True
        start = blob.find(normalized, start + len(normalized))
    return False
