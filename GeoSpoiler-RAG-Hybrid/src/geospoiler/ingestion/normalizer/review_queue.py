"""
Unified Review Queue — manages all items that require manual review.

Supported review types:
- ai_chat: Links to ChatGPT, Claude, Gemini conversations.
- external_link: Links to external websites that need human triage.
- uninformative: Posts flagged by auto-triage as having insufficient content.

All items are stored as .json files in output/review_queue/.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from geospoiler import config

logger = logging.getLogger("geospoiler.normalizer.review_queue")

# Review types
REVIEW_TYPE_AI_CHAT = "ai_chat"
REVIEW_TYPE_EXTERNAL_LINK = "external_link"
REVIEW_TYPE_UNINFORMATIVE = "uninformative"
REVIEW_TYPE_INSTAGRAM_LONG_REEL = "instagram_long_reel"
REVIEW_TYPE_ENRICHMENT_FAILURE = "enrichment_failure"


@dataclass
class ReviewResult:
    """Result of routing an item into the review queue."""

    placeholder_text: str
    action: str  # queued | already_reviewed
    filepath: str


def queue_item(
    *,
    review_type: str,
    channel_name: str,
    message_id: int,
    message_text: str = "",
    message_date: datetime | None = None,
    url: str = "",
    reason: str = "",
    normalized_filepath: str = "",
    reopen_processed: bool = False,
) -> ReviewResult:
    """
    Save an item to the review queue for manual processing.

    Creates a .json file in output/review_queue/ with all context.
    Returns queue metadata plus a placeholder text for the normalized output.
    """
    review_item = {
        "review_type": review_type,
        "url": url,
        "channel": channel_name,
        "message_id": message_id,
        "message_text": message_text,
        "message_date": message_date.isoformat() if message_date else None,
        "reason": reason,
        "normalized_filepath": normalized_filepath,
        "queued_at": datetime.now(UTC).isoformat(),
        "status": "pending",  # pending | processed | skipped
        "extracted_text": None,  # User fills this in
        "extraction_prompt": None,  # Optional reviewer instruction used for extraction
        "extraction_source": None,  # Source kind used for prompt extraction
        "attached_image": None,  # Path to user-attached image
    }

    # Build a unique filename.
    # For URL-based items, include a URL hash to distinguish multiple links in one post.
    # For non-URL items, use review_type as the discriminator.
    if url:
        unique_key = hashlib.sha1(url.encode()).hexdigest()[:8]
    else:
        unique_key = review_type

    filename = f"{channel_name}_{message_id}_{unique_key}.json"
    filepath = config.REVIEW_QUEUE_DIR / _sanitize(filename)

    # Guard: do NOT overwrite files that have already been reviewed unless an
    # automated extractor explicitly reopens a processed item after a fresh
    # source-access failure. Skipped items remain terminal by design.
    if filepath.exists():
        try:
            existing = json.loads(filepath.read_text(encoding="utf-8"))
            if existing.get("status") == "processed" and reopen_processed:
                review_item["reopened_from_status"] = "processed"
                review_item["previous_extracted_text"] = existing.get("extracted_text")
                review_item["previous_reviewed_at"] = existing.get("reviewed_at")
                logger.info("  Reopening processed review item after fresh extraction failure: %s", filepath.name)
            elif existing.get("status") == "pending":
                for key in ("reopened_from_status", "previous_extracted_text", "previous_reviewed_at"):
                    if key in existing:
                        review_item[key] = existing[key]
            elif existing.get("status") in ("processed", "skipped"):
                logger.info(
                    f"  Review item already reviewed ({existing['status']}), skipping: {filepath.name}"
                )
                return ReviewResult(
                    placeholder_text=_already_reviewed_placeholder(review_type, url, filepath.name),
                    action="already_reviewed",
                    filepath=str(filepath),
                )
        except (json.JSONDecodeError, OSError):
            pass  # Corrupted file — overwrite is fine

    filepath.write_text(
        json.dumps(review_item, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logger.info(f"  Review item ({review_type}) queued: {url or reason} -> {filepath}")

    return ReviewResult(
        placeholder_text=_queued_placeholder(review_type, url, filepath.name, reason),
        action="queued",
        filepath=str(filepath),
    )


def get_pending_reviews() -> list[dict]:
    """List all pending review items across all types."""
    items = []
    for f in config.REVIEW_QUEUE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("status") == "pending":
                data["_filepath"] = str(f)
                items.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return items


def mark_reviewed(
    filepath: str,
    extracted_text: str | None = None,
    skip: bool = False,
    attached_image: str | None = None,
    extraction_prompt: str | None = None,
    extraction_source: str | None = None,
) -> None:
    """
    Mark a review item as processed or skipped.

    Args:
        filepath: Path to the review .json file.
        extracted_text: The text to use (if processed).
        skip: If True, mark as skipped (no text extracted).
        attached_image: Optional path to an attached image file.
        extraction_prompt: Optional reviewer instruction used to produce extracted_text.
        extraction_source: Source kind used for prompt extraction.
    """
    path = config.resolve_project_path(filepath)
    if not path.exists():
        logger.warning(f"Review file not found: {filepath}")
        return

    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "skipped" if skip else "processed"
    data["extracted_text"] = extracted_text
    data["attached_image"] = attached_image
    if extraction_prompt is not None or "extraction_prompt" in data:
        data["extraction_prompt"] = extraction_prompt
    if extraction_source is not None or "extraction_source" in data:
        data["extraction_source"] = extraction_source
    data["reviewed_at"] = datetime.now(UTC).isoformat()

    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _already_reviewed_placeholder(review_type: str, url: str, filename: str) -> str:
    """Build placeholder text for already-reviewed items."""
    label = _type_label(review_type)
    if url:
        return f"[{label}: {url}]\n[Уже обработано: {filename}]"
    return f"[{label}]\n[Уже обработано: {filename}]"


def _queued_placeholder(review_type: str, url: str, filename: str, reason: str) -> str:
    """Build placeholder text for newly queued items."""
    label = _type_label(review_type)
    if url:
        return f"[{label}: {url}]\n[Отправлено в очередь на ручной просмотр: {filename}]"
    return f"[{label}: {reason}]\n[Отправлено в очередь на ручной просмотр: {filename}]"


def _type_label(review_type: str) -> str:
    """Human-readable label for the review type."""
    labels = {
        REVIEW_TYPE_AI_CHAT: "AI-диалог",
        REVIEW_TYPE_EXTERNAL_LINK: "Внешняя ссылка",
        REVIEW_TYPE_UNINFORMATIVE: "Малоинформативный пост",
        REVIEW_TYPE_INSTAGRAM_LONG_REEL: "Длинный Instagram Reel",
        REVIEW_TYPE_ENRICHMENT_FAILURE: "Ошибка обогащения",
    }
    return labels.get(review_type, "Ревью")


def _sanitize(name: str) -> str:
    """Sanitize filename."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name).strip("_")
