"""
Repair pass — asks LLM to fix specific contract violations.

Max 1 repair attempt per job. Unrepaired structural errors fail the job.
Unrepaired semantic errors may proceed with "extraction_unstable" only when
the payload still contains usable summary or key_points content.
"""

import json
import logging
from dataclasses import dataclass

from pydantic import ValidationError

from geospoiler.contracts.models import LLMPayload
from geospoiler.ingestion.enricher.llm_enricher import EmptyLLMResponseError, _call_llm, _normalize_to_payload
from geospoiler.ingestion.enricher.validator import ValidationResult, validate_payload

logger = logging.getLogger("geospoiler.enricher.repair")

_REPAIR_SYSTEM = """\
You are a JSON repair module. The extraction below violates contract rules.
Fix ONLY the listed violations. Do not add new information. Do not re-extract.
When a semantic field violates the Russian-language contract, translate or
rewrite only that semantic field in Russian. Preserve verbatim quotes and entity
surface forms in their dedicated fields and preserve the source meaning.
Return the corrected full JSON payload. No markdown. No commentary."""

_REPAIR_USER = """\
The following JSON extraction has these violations:
{violations}

Original JSON:
{payload_json}

Return the corrected JSON only. Fix only the violations listed above."""

_STRUCTURAL_REPAIR_SYSTEM = """\
You are a JSON schema repair module for an extraction pipeline.
Fix only the structural schema errors listed by the caller.
Do not add facts, context, aliases, interpretations, or external knowledge.
Do not fact-check or change the meaning of extracted content.
If a value cannot be represented without inventing content, remove that value or
use the schema default. Return the corrected full JSON payload only."""

_STRUCTURAL_REPAIR_USER = """\
The following extraction JSON does not match the required LLMPayload schema.

Schema errors:
{errors}

Original JSON:
{payload_json}

Return corrected JSON only. Fix structure only and do not add information."""


@dataclass
class RepairContext:
    """One shared repair budget for a complete enrichment job."""

    attempted: bool = False
    succeeded: bool = False


class StructuralRepairError(RuntimeError):
    """Raised when invalid raw output cannot be repaired within the job budget."""


def repair_invalid_payload(
    raw: object,
    error: ValidationError | EmptyLLMResponseError,
    context: RepairContext,
) -> LLMPayload:
    """Use the job's single repair attempt to fix a structural payload error."""
    if context.attempted:
        raise StructuralRepairError(
            "LLMPayload schema validation failed after the job repair budget was used"
        ) from error

    context.attempted = True
    if isinstance(error, ValidationError):
        errors = "\n".join(
            f"- {'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        )
    else:
        errors = f"- payload: {error}"
    raw_repaired = _call_llm(
        system=_STRUCTURAL_REPAIR_SYSTEM,
        user=_STRUCTURAL_REPAIR_USER.format(
            errors=errors,
            payload_json=json.dumps(raw, ensure_ascii=False, indent=2),
        ),
    )
    if not raw_repaired:
        raise StructuralRepairError("Structural repair returned no JSON payload") from error

    try:
        repaired = _normalize_to_payload(raw_repaired)
    except (ValidationError, EmptyLLMResponseError) as repair_error:
        raise StructuralRepairError(
            "Structural repair returned an invalid LLMPayload"
        ) from repair_error
    context.succeeded = True
    return repaired


def repair_if_needed(
    payload: LLMPayload,
    validation: ValidationResult,
    clean_text: str,
    ignored_block_texts: list[str] | None = None,
    context: RepairContext | None = None,
) -> tuple[LLMPayload, bool]:
    """
    Attempt one repair pass if validation found violations.

    Returns:
        (payload, repair_succeeded)
    """
    if validation.is_valid or not validation.should_repair:
        return payload, False

    repair_context = context or RepairContext()
    if repair_context.attempted:
        if "extraction_unstable" not in payload.quality_flags:
            payload.quality_flags.append("extraction_unstable")
        return payload, False

    repair_context.attempted = True

    violations_text = "\n".join(f"- {v}" for v in validation.violations)
    payload_json = payload.model_dump_json(indent=2)

    logger.info(f"Attempting repair for {len(validation.violations)} violation(s)")

    raw = _call_llm(
        system=_REPAIR_SYSTEM,
        user=_REPAIR_USER.format(
            violations=violations_text,
            payload_json=payload_json,
        ),
    )

    if not raw:
        logger.warning("Repair LLM call returned empty. Keeping original with flag.")
        if "extraction_unstable" not in payload.quality_flags:
            payload.quality_flags.append("extraction_unstable")
        return payload, False

    try:
        repaired = _normalize_to_payload(raw)
    except (ValidationError, EmptyLLMResponseError) as exc:
        logger.warning(f"Repair returned invalid v2 payload: {exc}")
        if "extraction_unstable" not in payload.quality_flags:
            payload.quality_flags.append("extraction_unstable")
        return payload, False

    revalidation = validate_payload(repaired, clean_text, ignored_block_texts)
    if not revalidation.is_valid:
        logger.warning(
            f"Repair still has {len(revalidation.violations)} violation(s). "
            "Post-validation fallback will decide whether the card is usable."
        )
        if "extraction_unstable" not in repaired.quality_flags:
            repaired.quality_flags.append("extraction_unstable")
        return repaired, False

    repair_context.succeeded = True
    return repaired, True
