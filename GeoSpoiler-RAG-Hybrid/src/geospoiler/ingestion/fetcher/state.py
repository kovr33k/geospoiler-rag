"""
Progress tracker for resumable fetching.
Stores the last processed message ID per channel so we can resume after crashes.

Key design decisions:
- State is keyed by channel_id (stable integer), NOT channel title (editable/non-unique).
- Writes go through a temp file → atomic rename so a crash mid-write cannot corrupt state.
- JSONDecodeError on load returns empty state rather than crashing the whole pipeline.
"""

import json
import os
from datetime import UTC, datetime

from geospoiler import config

PROGRESS_FILE = config.STATE_DIR / "progress.json"


def _load() -> dict:
    """Load progress state from disk. Returns empty state on any read/parse error."""
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupted file — start fresh rather than crashing the pipeline.
            # The old file is left in place so the user can inspect it.
            import logging
            logging.getLogger("geospoiler.state").warning(
                "progress.json is unreadable (corrupt or partial write). "
                "Starting with empty state. Original file kept as-is."
            )
    return {"channels": {}, "last_run": None}


def _save(state: dict) -> None:
    """Persist progress state to disk via atomic rename (crash-safe)."""
    state["last_run"] = datetime.now(UTC).isoformat()
    tmp_path = PROGRESS_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # os.replace is atomic on all platforms supported by CPython.
    os.replace(tmp_path, PROGRESS_FILE)


# ── Public API — all callers use channel_id (int) as the stable key ──

def get_last_message_id(channel_id: int) -> int:
    """
    Get the last processed message ID for a channel.
    Returns 0 if the channel has never been processed.
    """
    state = _load()
    key = str(channel_id)  # JSON keys are always strings
    channel_data = state.get("channels", {}).get(key, {})
    return channel_data.get("last_message_id", 0)


def set_last_message_id(channel_id: int, channel_title: str, message_id: int) -> None:
    """
    Update the last processed message ID for a channel.
    channel_title is stored for human readability only — never used as a lookup key.
    """
    state = _load()
    if "channels" not in state:
        state["channels"] = {}
    key = str(channel_id)
    if key not in state["channels"]:
        state["channels"][key] = {}
    state["channels"][key]["title"] = channel_title          # informational only
    state["channels"][key]["last_message_id"] = message_id
    state["channels"][key]["updated_at"] = datetime.now(UTC).isoformat()
    _save(state)


def get_all_progress() -> dict:
    """Return the full progress state."""
    return _load()


def mark_message_processed(channel_id: int, channel_title: str, message_id: int) -> None:
    """
    Mark a specific message as processed.
    Updates last_message_id only if this message_id is higher (messages are in order).
    """
    current = get_last_message_id(channel_id)
    if message_id > current:
        set_last_message_id(channel_id, channel_title, message_id)
