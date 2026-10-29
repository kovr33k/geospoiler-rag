"""Журнал исходов: open processing outcomes for posts that did not become cards.

The file is ``config.STATE_DIR / ingestion_outcomes.json``. It keeps only open
outcomes. An outcome leaves the journal when that post is later processed
successfully. Post keys are ``channel_id:message_id``. Channel-level keys
(no message id) are ``channel_id`` and are reserved for later stages.
"""

import json
import logging
from datetime import UTC, datetime

from geospoiler import config

logger = logging.getLogger("geospoiler.outcomes")

OUTCOMES_FILENAME = "ingestion_outcomes.json"


def _outcomes_path():
    """Resolve the journal path at call time so tests can redirect STATE_DIR."""
    return config.STATE_DIR / OUTCOMES_FILENAME


def _outcome_key(channel_id: int, message_id: int | None) -> str:
    if message_id is None:
        return str(channel_id)
    return f"{channel_id}:{message_id}"


def _empty() -> dict:
    return {"outcomes": {}}


def _load() -> dict:
    path = _outcomes_path()
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning(
            "ingestion_outcomes.json is unreadable. Starting with an empty journal."
        )
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    outcomes = data.get("outcomes")
    if not isinstance(outcomes, dict):
        data["outcomes"] = {}
    return data


def _save(state: dict) -> None:
    """Persist the journal via a temporary file and replace, like enrichment progress."""
    path = _outcomes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def failure_reason(exc: Exception) -> str:
    """Exception type plus the enricher's redacted failure message."""
    from geospoiler.ingestion.enricher.pipeline import _safe_failure_message

    return f"{type(exc).__name__}: {_safe_failure_message(exc)}"


def record_open_outcome(
    *,
    channel_id: int,
    channel_title: str,
    channel_username: str,
    message_id: int | None,
    post_url: str | None,
    stage: str,
    outcome: str,
    reason: str,
) -> None:
    """Record an open outcome. Repeats increment attempts and keep first_seen."""
    state = _load()
    outcomes = state["outcomes"]
    key = _outcome_key(channel_id, message_id)
    now = datetime.now(UTC).isoformat()
    existing = outcomes.get(key) or {}
    attempts = existing.get("attempts", 0) + 1
    first_seen = existing.get("first_seen") or now
    outcomes[key] = {
        "channel_id": channel_id,
        "channel_title": channel_title,
        "channel_username": channel_username,
        "message_id": message_id,
        "post_url": post_url,
        "stage": stage,
        "outcome": outcome,
        "reason": reason,
        "attempts": attempts,
        "first_seen": first_seen,
        "last_seen": now,
    }
    _save(state)


def close_outcome(channel_id: int, message_id: int | None) -> None:
    """Drop an open outcome after the post is processed successfully."""
    path = _outcomes_path()
    if not path.exists():
        return
    state = _load()
    key = _outcome_key(channel_id, message_id)
    if key not in state["outcomes"]:
        return
    del state["outcomes"][key]
    _save(state)


def read_open_outcomes() -> list[dict]:
    """Return the open outcome records. The journal is not modified."""
    outcomes = _load()["outcomes"]
    records = []
    for record in outcomes.values():
        if isinstance(record, dict):
            records.append(dict(record))
    return records
