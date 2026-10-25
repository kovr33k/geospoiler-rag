"""Deterministic claim occurrence blueprints and reusable extraction artifacts."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from geospoiler.wiki.hashing import canonical_json, canonicalize, content_fingerprint, content_hash, normalize_text
from geospoiler.wiki.schema import CLAIM_EXTRACTION_STAGE_KIND
from geospoiler.wiki.state import StateConflictError, StateNotFoundError, _immediate_transaction, _utc_now

OCCURRENCE_SCHEMA_VERSION = "occurrence:v1"
BLUEPRINT_SCHEMA_VERSION = "occurrence-blueprint:v1"

_FIELD_SOURCES = (
    ("key_point", "key_points"),
    ("thesis", "theses"),
    ("quote", "quotes"),
    ("event", "events"),
)
_CLOCK_TIMESTAMP_RE = re.compile(
    r"^(?:(?P<hours>\d+):)?(?P<minutes>[0-5]?\d):"
    r"(?P<seconds>[0-5]\d)(?:\.(?P<fraction>\d{1,9}))?$"
)
_ISO_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$"
)


@dataclass(frozen=True)
class OccurrenceBlueprint:
    field_kind: str
    exact_payload: dict[str, Any]
    exact_payload_json: str
    exact_payload_hash: str
    occurrence_fingerprint: str
    locator: dict[str, Any]
    locator_json: str
    evidence_metadata: dict[str, Any]
    duplicate_ordinal: int
    item_ordinal: int = -1

    @property
    def has_exact_external_locator(self) -> bool:
        return bool(self.locator.get("exact_external"))

    @property
    def external_locator_key(self) -> str | None:
        if not self.has_exact_external_locator:
            return None
        return canonical_json(
            {
                "locator_type": self.locator.get("locator_type"),
                "value": self.locator.get("value"),
            }
        )


@dataclass(frozen=True)
class ExtractionArtifact:
    extraction_artifact_id: str
    extraction_artifact_key: str
    processor_contract_version_id: str
    processor_contract_hash: str
    claim_inputs_hash: str
    artifact_hash: str
    artifact_json: str
    items: tuple[OccurrenceBlueprint, ...]


def build_occurrence_blueprints(
    claim_inputs: Mapping[str, Any],
) -> tuple[OccurrenceBlueprint, ...]:
    """Extract stable occurrence blueprints; summary is intentionally ignored."""
    provisional: list[OccurrenceBlueprint] = []
    for field_kind, collection_name in _FIELD_SOURCES:
        values = claim_inputs.get(collection_name, ())
        if not isinstance(values, Sequence) or isinstance(values, str | bytes | bytearray):
            raise StateConflictError(f"{collection_name} must be an array")
        for value in values:
            if not isinstance(value, Mapping):
                raise StateConflictError(f"{collection_name} items must be objects")
            exact_payload = {
                key: item
                for key, item in value.items()
                if key not in {"external_locator", "source_span", "timestamp"}
            }
            exact_quote_paths = (("text",),) if field_kind == "quote" else ()
            unordered_paths = (("actors",),) if field_kind == "event" else ()
            exact_payload_json = canonical_json(
                exact_payload,
                unordered_collection_paths=unordered_paths,
                exact_quote_paths=exact_quote_paths,
            )
            payload_hash = content_hash(
                exact_payload,
                namespace=f"wiki-occurrence-payload:v1:{field_kind}",
                unordered_collection_paths=unordered_paths,
                exact_quote_paths=exact_quote_paths,
            )
            fingerprint = _occurrence_fingerprint(field_kind, exact_payload)
            base_locator = _exact_locator(value)
            if base_locator is None:
                base_locator = {
                    "locator_kind": "content_fingerprint",
                    "field_kind": field_kind,
                    "content_fingerprint": content_fingerprint(
                        exact_payload,
                        unordered_collection_paths=unordered_paths,
                        exact_quote_paths=exact_quote_paths,
                    ),
                    "exact_external": False,
                }
            provisional.append(
                OccurrenceBlueprint(
                    field_kind=field_kind,
                    exact_payload=canonicalize(
                        exact_payload,
                        unordered_collection_paths=unordered_paths,
                        exact_quote_paths=exact_quote_paths,
                    ),
                    exact_payload_json=exact_payload_json,
                    exact_payload_hash=payload_hash,
                    occurrence_fingerprint=fingerprint,
                    locator=base_locator,
                    locator_json="",
                    evidence_metadata={
                        "blueprint_schema_version": BLUEPRINT_SCHEMA_VERSION,
                    },
                    duplicate_ordinal=0,
                )
            )

    grouped: dict[tuple[str, str, str], list[OccurrenceBlueprint]] = defaultdict(list)
    for item in provisional:
        grouped[
            (
                item.field_kind,
                canonical_json(item.locator),
                item.exact_payload_json,
            )
        ].append(item)

    with_duplicates: list[OccurrenceBlueprint] = []
    for group_key in sorted(grouped):
        for duplicate_ordinal, item in enumerate(grouped[group_key]):
            locator = {**item.locator, "duplicate_ordinal": duplicate_ordinal}
            with_duplicates.append(
                replace(
                    item,
                    locator=locator,
                    locator_json=canonical_json(locator),
                    evidence_metadata={
                        **item.evidence_metadata,
                        "duplicate_ordinal": duplicate_ordinal,
                    },
                    duplicate_ordinal=duplicate_ordinal,
                )
            )

    ordered = sorted(
        with_duplicates,
        key=lambda item: (
            item.field_kind,
            item.locator_json,
            item.exact_payload_hash,
            item.occurrence_fingerprint,
        ),
    )
    return tuple(replace(item, item_ordinal=index) for index, item in enumerate(ordered))


def build_extraction_artifact(
    *,
    claim_inputs: Mapping[str, Any],
    claim_inputs_hash: str,
    processor_contract_version_id: str,
    processor_contract_hash: str,
) -> ExtractionArtifact:
    """Build a lineage-free artifact keyed only by claim input and active method."""
    items = build_occurrence_blueprints(claim_inputs)
    key_payload = {
        "claim_inputs_hash": claim_inputs_hash,
        "processor_contract_hash": processor_contract_hash,
    }
    key_digest = content_hash(
        key_payload,
        namespace="wiki-extraction-artifact-key:v1",
    ).removeprefix("sha256:")
    artifact_key = f"extract-key:v1:sha256:{key_digest}"
    artifact_payload = {
        "schema_version": BLUEPRINT_SCHEMA_VERSION,
        **key_payload,
        "items": [
            {
                "item_ordinal": item.item_ordinal,
                "field_kind": item.field_kind,
                "exact_payload": item.exact_payload,
                "exact_payload_hash": item.exact_payload_hash,
                "occurrence_fingerprint": item.occurrence_fingerprint,
                "locator": item.locator,
                "evidence_metadata": item.evidence_metadata,
            }
            for item in items
        ],
    }
    artifact_json = canonical_json(artifact_payload)
    artifact_hash = content_hash(
        artifact_payload,
        namespace="wiki-extraction-artifact:v1",
    )
    artifact_id = f"extract-artifact:v1:sha256:{artifact_hash.removeprefix('sha256:')}"
    return ExtractionArtifact(
        extraction_artifact_id=artifact_id,
        extraction_artifact_key=artifact_key,
        processor_contract_version_id=processor_contract_version_id,
        processor_contract_hash=processor_contract_hash,
        claim_inputs_hash=claim_inputs_hash,
        artifact_hash=artifact_hash,
        artifact_json=artifact_json,
        items=items,
    )


def store_extraction_artifact(
    connection: sqlite3.Connection,
    artifact: ExtractionArtifact,
) -> ExtractionArtifact:
    """Idempotently persist one immutable reusable artifact and its ordered items."""
    with _immediate_transaction(connection):
        contract = connection.execute(
            """
            SELECT stage_kind, contract_hash
            FROM processor_contract_versions
            WHERE processor_contract_version_id = ?
            """,
            (artifact.processor_contract_version_id,),
        ).fetchone()
        if contract is None:
            raise StateNotFoundError(
                f"Unknown processor contract {artifact.processor_contract_version_id}"
            )
        if contract["stage_kind"] != CLAIM_EXTRACTION_STAGE_KIND:
            raise StateConflictError("Extraction artifacts require claim_extraction contract")
        if contract["contract_hash"] != artifact.processor_contract_hash:
            raise StateConflictError("Extraction artifact contract hash does not match its version")

        existing = connection.execute(
            """
            SELECT
                extraction_artifact_id,
                processor_contract_version_id,
                claim_inputs_hash,
                artifact_hash,
                artifact_json
            FROM extraction_artifacts
            WHERE extraction_artifact_key = ?
            """,
            (artifact.extraction_artifact_key,),
        ).fetchone()
        if existing is not None:
            expected = (
                artifact.extraction_artifact_id,
                artifact.processor_contract_version_id,
                artifact.claim_inputs_hash,
                artifact.artifact_hash,
                artifact.artifact_json,
            )
            actual = tuple(existing)
            if actual != expected:
                raise StateConflictError(
                    f"Artifact key {artifact.extraction_artifact_key!r} has different content"
                )
            return load_extraction_artifact(
                connection,
                extraction_artifact_id=artifact.extraction_artifact_id,
            )

        now = _utc_now()
        connection.execute(
            """
            INSERT INTO extraction_artifacts (
                extraction_artifact_id,
                extraction_artifact_key,
                processor_contract_version_id,
                claim_inputs_hash,
                artifact_hash,
                artifact_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact.extraction_artifact_id,
                artifact.extraction_artifact_key,
                artifact.processor_contract_version_id,
                artifact.claim_inputs_hash,
                artifact.artifact_hash,
                artifact.artifact_json,
                now,
            ),
        )
        for item in artifact.items:
            item_id = (
                f"extract-item:v1:{artifact.extraction_artifact_id}:"
                f"{item.item_ordinal}"
            )
            connection.execute(
                """
                INSERT INTO extraction_artifact_items (
                    extraction_artifact_item_id,
                    extraction_artifact_id,
                    item_ordinal,
                    field_kind,
                    exact_payload_json,
                    exact_payload_hash,
                    occurrence_fingerprint,
                    locator_json,
                    evidence_metadata_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    artifact.extraction_artifact_id,
                    item.item_ordinal,
                    item.field_kind,
                    item.exact_payload_json,
                    item.exact_payload_hash,
                    item.occurrence_fingerprint,
                    item.locator_json,
                    canonical_json(item.evidence_metadata),
                    now,
                ),
            )
        return artifact


def load_extraction_artifact(
    connection: sqlite3.Connection,
    *,
    extraction_artifact_id: str,
) -> ExtractionArtifact:
    """Load one immutable artifact and reconstruct typed blueprints."""
    row = connection.execute(
        """
        SELECT artifact.*, contract.contract_hash
        FROM extraction_artifacts AS artifact
        JOIN processor_contract_versions AS contract
          ON contract.processor_contract_version_id =
             artifact.processor_contract_version_id
        WHERE artifact.extraction_artifact_id = ?
        """,
        (extraction_artifact_id,),
    ).fetchone()
    if row is None:
        raise StateNotFoundError(f"Unknown extraction artifact {extraction_artifact_id}")
    item_rows = connection.execute(
        """
        SELECT *
        FROM extraction_artifact_items
        WHERE extraction_artifact_id = ?
        ORDER BY item_ordinal
        """,
        (extraction_artifact_id,),
    ).fetchall()
    items = tuple(
        OccurrenceBlueprint(
            field_kind=item["field_kind"],
            exact_payload=json.loads(item["exact_payload_json"]),
            exact_payload_json=item["exact_payload_json"],
            exact_payload_hash=item["exact_payload_hash"],
            occurrence_fingerprint=item["occurrence_fingerprint"],
            locator=json.loads(item["locator_json"]),
            locator_json=item["locator_json"],
            evidence_metadata=json.loads(item["evidence_metadata_json"]),
            duplicate_ordinal=int(json.loads(item["locator_json"]).get("duplicate_ordinal", 0)),
            item_ordinal=int(item["item_ordinal"]),
        )
        for item in item_rows
    )
    return ExtractionArtifact(
        extraction_artifact_id=row["extraction_artifact_id"],
        extraction_artifact_key=row["extraction_artifact_key"],
        processor_contract_version_id=row["processor_contract_version_id"],
        processor_contract_hash=row["contract_hash"],
        claim_inputs_hash=row["claim_inputs_hash"],
        artifact_hash=row["artifact_hash"],
        artifact_json=row["artifact_json"],
        items=items,
    )


def _occurrence_fingerprint(field_kind: str, payload: Mapping[str, Any]) -> str:
    fingerprint_payload = {
        "field_kind": field_kind,
        "text": payload.get("text"),
        "description": payload.get("description"),
        "speaker": payload.get("speaker"),
        "event_type": payload.get("event_type"),
        "date": payload.get("date_normalized") or payload.get("date_text"),
        "modality": payload.get("modality"),
        "type": payload.get("type"),
        "stance": payload.get("stance"),
    }
    return content_hash(
        fingerprint_payload,
        namespace="wiki-occurrence-fingerprint:v1",
        exact_quote_paths=(("text",),) if field_kind == "quote" else (),
    )


def _exact_locator(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Recognize only occurrence-level locators; card/segment metadata is never passed here."""
    external = payload.get("external_locator")
    if isinstance(external, str) and (normalized_external := normalize_text(external)):
        return {
            "locator_kind": "external",
            "locator_type": "external_locator",
            "value": normalized_external,
            "exact_external": True,
        }
    normalized_span = _validated_source_span(payload.get("source_span"))
    if normalized_span is not None:
        return {
            "locator_kind": "external",
            "locator_type": "source_span",
            "value": canonicalize(normalized_span),
            "exact_external": True,
        }
    normalized_timestamp = _validated_timestamp(payload.get("timestamp"))
    if normalized_timestamp is not None:
        return {
            "locator_kind": "external",
            "locator_type": "timestamp",
            "value": normalized_timestamp,
            "exact_external": True,
        }
    return None


def _validated_source_span(value: Any) -> dict[str, int | float] | None:
    start: Any
    end: Any
    if isinstance(value, Mapping):
        if "start" not in value or "end" not in value:
            return None
        if "approximate" in value:
            return None
        if "exact" in value and value["exact"] is not True:
            return None
        start, end = value["start"], value["end"]
    elif isinstance(value, list | tuple) and len(value) == 2:
        start, end = value
    else:
        return None
    if not _is_finite_nonnegative_number(start):
        return None
    if not _is_finite_nonnegative_number(end) or end < start:
        return None
    return {"start": start, "end": end}


def _validated_timestamp(value: Any) -> int | float | str | None:
    if _is_finite_nonnegative_number(value):
        return value
    if not isinstance(value, str):
        return None
    normalized = normalize_text(value)
    if not normalized:
        return None
    if _CLOCK_TIMESTAMP_RE.fullmatch(normalized):
        return normalized
    if not _ISO_TIMESTAMP_RE.fullmatch(normalized):
        return None
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    return normalized


def _is_finite_nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )
