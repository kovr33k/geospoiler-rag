"""
AI Chat Handler - routes AI conversation links to a manual review queue.

Links to ChatGPT, Claude, and Gemini conversations are saved as review items.
The user must manually extract the relevant text and place it in the normalized output.

This module is a thin backward-compatible wrapper around review_queue.py.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from geospoiler.ingestion.normalizer.review_queue import (
    REVIEW_TYPE_AI_CHAT,
    ReviewResult,
    queue_item,
)

logger = logging.getLogger("geospoiler.normalizer.ai_chat")


@dataclass
class AIReviewResult:
    """Result of routing an AI chat URL into the manual review queue."""

    placeholder_text: str
    action: str  # queued | already_reviewed
    filepath: str


def queue_for_review(
    url: str,
    channel_name: str,
    message_id: int,
    message_text: str = "",
    message_date: datetime | None = None,
) -> AIReviewResult:
    """
    Save an AI chat link to the review queue for manual processing.

    Creates a .json file in output/review_queue/ with all context.
    Returns queue metadata plus a placeholder text for the normalized output.
    """
    result: ReviewResult = queue_item(
        review_type=REVIEW_TYPE_AI_CHAT,
        channel_name=channel_name,
        message_id=message_id,
        message_text=message_text,
        message_date=message_date,
        url=url,
        reason="AI chat link",
    )

    return AIReviewResult(
        placeholder_text=result.placeholder_text,
        action=result.action,
        filepath=result.filepath,
    )
