"""Stamp the user-attested release risk register into the common evidence envelope."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.operations.release_identity import (
    SafeArgumentParser,
    assert_no_release_secrets,
    atomic_write_release_json,
    canonical_json,
    preflight_release_identity,
    run_release_cli,
    safe_resolve_release_path,
    stamp_report,
    strict_json_loads,
)

_ROOT_FIELDS = {"risks"}
_RISK_FIELDS = {"id", "status", "completion_basis", "title", "owner", "deadline", "notes"}
_TOKEN_RISK_ID = "RISK-TOKEN-ROTATION-001"


def validate_risk_register_payload(payload: Any) -> list[dict[str, str]]:
    """Validate the exact V1 risk contract and return a detached copy."""
    if not isinstance(payload, dict):
        raise ValueError("risk register root must be an object")
    assert_no_release_secrets(canonical_json(payload), label="risk_register_input")
    unknown_root = set(payload) - _ROOT_FIELDS
    missing_root = _ROOT_FIELDS - set(payload)
    if missing_root or unknown_root:
        raise ValueError(
            "risk register root fields invalid: "
            f"missing={','.join(sorted(missing_root)) or 'none'};"
            f"unknown={','.join(sorted(unknown_root)) or 'none'}"
        )
    risks = payload["risks"]
    if not isinstance(risks, list) or not risks:
        raise ValueError("risk register risks must be a non-empty list")
    validated: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(risks):
        if not isinstance(item, dict):
            raise ValueError(f"risk[{index}] must be an object")
        unknown = set(item) - _RISK_FIELDS
        missing = {"id", "status"} - set(item)
        if missing or unknown:
            raise ValueError(
                f"risk[{index}] fields invalid: missing={','.join(sorted(missing)) or 'none'};"
                f"unknown={','.join(sorted(unknown)) or 'none'}"
            )
        for field, value in item.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"risk[{index}].{field} must be a non-empty string")
        risk_id = item["id"].strip()
        if risk_id in seen_ids:
            raise ValueError(f"duplicate risk id: {risk_id}")
        seen_ids.add(risk_id)
        validated.append({field: item[field].strip() for field in item})
    token_risks = [item for item in validated if item["id"] == _TOKEN_RISK_ID]
    if len(token_risks) != 1:
        raise ValueError(f"risk register must contain exactly one {_TOKEN_RISK_ID}")
    token_risk = token_risks[0]
    if token_risk.get("status") != "closed" or token_risk.get("completion_basis") != "user_attestation":
        raise ValueError("token rotation risk must be closed by user_attestation")
    assert_no_release_secrets(canonical_json({"risks": validated}), label="risk_register")
    return validated


def main() -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--release-run-id", required=True)
    args = parser.parse_args()
    safe_resolve_release_path(args.input, label="risk_register_input_path")
    safe_resolve_release_path(args.output, label="risk_register_output_path")
    payload = strict_json_loads(args.input.read_text(encoding="utf-8"))
    risks = validate_risk_register_payload(payload)
    preflight = preflight_release_identity(args.release_run_id, args.manifest)
    identity = preflight.identity
    report = stamp_report(
        {
            "schema_version": "release_risk_register_v1",
            "risks": risks,
            "errors": [],
        },
        identity,
        f"python scripts/build_release_risk_register.py --input {args.input} --output {args.output} --manifest {args.manifest} --release-run-id {args.release_run_id}",
        producer="scripts.build_release_risk_register",
        producer_schema_version="release_risk_register_v1",
        source_inputs={
            "manifest_sha256": identity["manifest_sha256"],
            "corpus_identity_sha256": identity["corpus_identity_sha256"],
        },
        command_argv=[
            "python", "scripts/build_release_risk_register.py", "--input", str(args.input), "--output", str(args.output),
            "--manifest", str(args.manifest), "--release-run-id", args.release_run_id,
        ],
    )
    assert_no_release_secrets(canonical_json(report), label="stamped_risk_register")
    atomic_write_release_json(args.output, report, label="stamped_risk_register")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_release_cli(main, component="build_release_risk_register"))
