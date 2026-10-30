"""Read-only ingest report.

Collectors are independent. Each returns problem items. ``combine_problem_items``
merges them per (channel_id, message_id) and keeps one label, by priority
review → retry → failed → policy. Collectors are listed in ``COLLECTORS``.
"""

import copy
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from geospoiler import config
from geospoiler.ingestion.enricher import pipeline as enricher_pipeline
from geospoiler.ingestion.fetcher.state import get_last_message_id
from geospoiler.ingestion.normalizer.media_selection import caption_has_letters_or_digits
from geospoiler.ingestion.normalizer.pipeline import _sanitize
from geospoiler.ingestion.normalizer.review_queue import REVIEW_TYPE_ENRICHMENT_FAILURE, get_pending_reviews
from geospoiler.ingestion.outcomes import read_open_outcomes

logger = logging.getLogger("geospoiler.ingest_report")

CATEGORIES = ("review", "retry", "failed", "policy")
CATEGORY_LABELS = {
    "review": "ожидает ревью",
    "retry": "ожидает повтора",
    "failed": "ошибка обработки",
    "policy": "пропуск по политике",
}
_CATEGORY_PRIORITY = {name: index for index, name in enumerate(CATEGORIES)}
_JOURNAL_FIELDS = (
    "channel_id", "channel_title", "message_id", "post_url", "stage", "reason", "attempts", "first_seen", "last_seen",
)
# Every item carries the full JSON shape; collectors fill only what they know.
_ITEM_DEFAULTS = dict.fromkeys(_JOURNAL_FIELDS + ("category",))

# Video, voice, audio and documents without a meaningful caption are policy
# skips. Images are not: a post «только изображение» is selected for analysis.
_POLICY_MEDIA_TYPES = {"video", "voice", "audio", "document"}
_UNSELECTED_REASON = "вложение не анализировалось по выборочной политике"


def _read_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def collect_journal_problems() -> list[dict]:
    """Open normalization and fetch failures; the next run retries both."""
    return [
        {**{name: record.get(name) for name in _JOURNAL_FIELDS}, "category": "retry"}
        for record in read_open_outcomes()
        if record.get("stage") in {"normalize", "fetch"} and record.get("outcome") == "failed"
    ]


def _caption_from_normalized_text(text: str) -> str:
    """User text, without the header and generated sections.

    Generated sections (header, placeholders, analysis blocks) are paragraphs
    that start with '['. Meaningful captions use caption_has_letters_or_digits.
    """
    kept = [paragraph for paragraph in text.split("\n\n") if not paragraph.lstrip().startswith("[")]
    return "\n\n".join(kept)


def _failure_detail(item: dict) -> str:
    """Download error, otherwise visual, extraction or transcription error."""
    first = ["error"] if item.get("download_status") == "failed" else []
    for name in first + ["visual_error", "text_extraction_error", "transcription_error", "error"]:
        if item.get(name):
            return str(item[name])
    return "ошибка обработки"


def _classify_attachment(item: dict, *, meaningful_caption: bool) -> tuple[str, str, str] | None:
    """Return category, media type and detail. A policy skip is never a failure."""
    media_type = item.get("media_type") or "attachment"
    download_status = item.get("download_status")
    error = item.get("error") or ""
    operations = item.get("analysis_operations")
    if download_status == "disabled":
        return "policy", media_type, error or "MEDIA_CAPTURE_ENABLED=false"
    if download_status == "skipped" and error.startswith("file_size_exceeds_limit"):
        return "policy", media_type, error
    failed = any(item.get(name) == "failed" for name in ("download_status", "analysis_status", "text_extraction_status"))
    if operations and failed:
        return "failed", media_type, _failure_detail(item)
    unselected = operations == [] or item.get("analysis_status") == "not_analyzed"
    if unselected and media_type in _POLICY_MEDIA_TYPES and not meaningful_caption:
        return "policy", media_type, _UNSELECTED_REASON
    return None


def _media_reason(problems: list[tuple[str, str]]) -> str:
    """Types and, when a post has several problem attachments, their count."""
    types: list[str] = []
    details: list[str] = []
    for media_type, detail in problems:
        if media_type not in types:
            types.append(media_type)
        if detail and detail not in details:
            details.append(detail)
    type_part = ", ".join(types)
    if len(problems) > 1:
        type_part = f"{type_part} ({len(problems)})"
    if not details:
        return type_part
    return f"{type_part}: {'; '.join(details)}"


def _media_problem_item(text_path: Path) -> dict | None:
    metadata = _read_json_object(text_path.with_suffix(".meta.json"))
    media = metadata.get("media")
    if not media:
        return None
    try:
        saved_text = text_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        logger.warning("Skipping unreadable normalized text: %s", text_path)
        return None
    meaningful = caption_has_letters_or_digits(_caption_from_normalized_text(saved_text))
    problems = [
        classified
        for attachment in media
        if (classified := _classify_attachment(attachment, meaningful_caption=meaningful)) is not None
    ]
    if not problems:
        return None
    category = "failed" if any(item[0] == "failed" for item in problems) else "policy"
    chosen = [(media_type, detail) for item_category, media_type, detail in problems if item_category == category]
    return {
        "channel_id": metadata.get("channel_id"),
        "channel_title": metadata.get("channel_name"),
        "message_id": metadata.get("message_id"),
        "post_url": metadata.get("post_url"),
        "stage": "media",
        "category": category,
        "reason": _media_reason(chosen),
    }


def collect_media_problems() -> list[dict]:
    """Attachment problems from normalized metadata. One item per post, stage media."""
    root = config.NORMALIZED_DIR
    if not root.is_dir():
        return []
    items = (_media_problem_item(text_path) for text_path in sorted(root.rglob("*.txt")))
    return [item for item in items if item is not None]


def collect_enrichment_problems() -> list[dict]:
    """Posts whose normalized material has no up-to-date Enriched v2 card."""
    if not config.NORMALIZED_DIR.is_dir():
        return []
    jobs = enricher_pipeline._collect_enrichment_jobs(
        progress=copy.deepcopy(enricher_pipeline._load_progress()),
        channel_filter=None,
        force=False,
        stats=enricher_pipeline.EnrichmentStats(),
        read_only=True,
    )
    items = []
    for job in jobs:
        meta = _read_json_object(job.meta_path)
        items.append({
            "channel_id": meta.get("channel_id"),
            "channel_title": meta.get("channel_name") or job.channel_name,
            "message_id": int(job.msg_id),
            "post_url": meta.get("post_url"),
            "stage": "enrich",
            "category": "retry",
            "reason": "нет актуальной карточки",
        })
    return items


def _review_reason(item: dict) -> str | None:
    review_type = str(item.get("review_type") or "").strip()
    detail = str(item.get("reason") or "").strip() or str(item.get("url") or "").strip()
    if review_type and detail:
        return f"{review_type}: {detail}"
    return review_type or detail or None


def collect_review_queue_problems() -> list[dict]:
    """Pending review items. Enrichment failures stay retry through enricher selection."""
    items = []
    for review in get_pending_reviews():
        if review.get("review_type") == REVIEW_TYPE_ENRICHMENT_FAILURE:
            continue
        channel, message_id = review.get("channel"), review.get("message_id")
        # Review files store the channel name; its metadata holds the id and real title.
        meta = {}
        if channel and message_id is not None:
            meta = _read_json_object(config.NORMALIZED_DIR / _sanitize(channel) / f"{message_id}.meta.json")
        items.append({
            "channel_id": meta.get("channel_id"),
            "channel_title": meta.get("channel_name") or channel,
            "message_id": message_id,
            "post_url": meta.get("post_url"),
            "stage": "review",
            "category": "review",
            "reason": _review_reason(review),
        })
    return items


def _telegram_side(card: dict) -> dict:
    """Telegram channel title and post link of a card; a video names its post in source_chain."""
    provenance = card.get("provenance") if isinstance(card.get("provenance"), dict) else {}
    if card.get("content_type") != "youtube_transcript":
        return {"channel_title": provenance.get("channel"), "post_url": provenance.get("post_url")}
    links = (card.get("source_chain") or {}).get("external_links") or []
    post_url = next((link.get("url") for link in links if link.get("label") == "Telegram post"), None)
    channel_title = None
    if provenance.get("normalized_path"):
        meta_dir = config.resolve_project_path(provenance["normalized_path"]).parent
        for meta_path in sorted(meta_dir.glob("*.youtube.meta.json")):
            channel_title = (_read_json_object(meta_path).get("telegram_source") or {}).get("channel_name")
    return {"channel_title": channel_title, "post_url": post_url}


def collect_exact_duplicates() -> list[dict]:
    """Sources kept as exact copies of another source. They are not problems."""
    if not config.ENRICHED_DIR.is_dir():
        return []
    cards = {}
    for card_path in sorted(config.ENRICHED_DIR.rglob("*.enriched.json")):
        card = _read_json_object(card_path)
        source_id = (card.get("provenance") or {}).get("source_id")
        if source_id:
            cards[source_id] = card
    items = []
    for source_id, card in cards.items():
        root_id = card.get("duplicate_of")
        if not root_id:
            continue
        root = _telegram_side(cards.get(root_id, {}))
        items.append({
            "kind": "video" if card.get("content_type") == "youtube_transcript" else "post",
            "source_id": source_id,
            **_telegram_side(card),
            "duplicate_of": root_id,
            "root_channel_title": root["channel_title"],
            "root_post_url": root["post_url"],
        })
    items.sort(key=lambda item: (item["channel_title"] or "", item["source_id"]))
    return items


def combine_problem_items(items: list[dict]) -> list[dict]:
    """Keep one item per channel and message, preferring the higher-priority label."""
    chosen: dict[tuple, dict] = {}
    for item in items:
        key = (item.get("channel_id"), item.get("message_id"))
        current = chosen.get(key)
        if current is None or _CATEGORY_PRIORITY[item["category"]] < _CATEGORY_PRIORITY[current["category"]]:
            chosen[key] = item
    return list(chosen.values())


def _with_cursor_flags(items: list[dict]) -> list[dict]:
    """A post blocks the channel cursor when its message id is still ahead of it."""
    cursors: dict[int, int] = {}
    for item in items:
        channel_id, message_id = item["channel_id"], item["message_id"]
        if channel_id is not None and channel_id not in cursors:
            cursors[channel_id] = get_last_message_id(channel_id)
        item["blocks_cursor"] = channel_id is not None and message_id is not None and message_id > cursors[channel_id]
    return items


def _summary(items: list[dict]) -> dict:
    by_category = {name: 0 for name in CATEGORIES}
    for item in items:
        category = item.get("category")
        if category in by_category:
            by_category[category] += 1
    return {"total": len(items), "by_category": by_category}


def build_ingest_report(*, channel: str | None = None, category: str | None = None) -> dict:
    """Build the report from registered collectors. This function only reads."""
    collected = [{**_ITEM_DEFAULTS, **item} for collect in COLLECTORS for item in collect()]
    items = _with_cursor_flags(combine_problem_items(collected))
    if channel is not None:
        items = [item for item in items if item["channel_title"] == channel]
    if category is not None:
        items = [item for item in items if item["category"] == category]
    items.sort(key=lambda item: (
        item["channel_title"] or "", item["channel_id"] or 0, item["message_id"] is None, item["message_id"] or 0,
    ))
    # Copies are not outcomes, so a category filter leaves them out.
    duplicates = [] if category is not None else collect_exact_duplicates()
    if channel is not None:
        duplicates = [item for item in duplicates if item["channel_title"] == channel]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": _summary(items),
        "items": items,
        "duplicates": duplicates,
    }


def _duplicates_line(duplicates: list[dict]) -> str:
    videos = sum(item["kind"] == "video" for item in duplicates)
    return (
        f"явных дублей: {len(duplicates)} (постов {len(duplicates) - videos}, видео {videos}); "
        "в поиске не участвуют, Luna на них не тратилась"
    )


def render_ingest_report(report: dict) -> str:
    """Console listing grouped by channel, with Russian labels and totals."""
    lines: list[str] = []
    grouped: dict[tuple, list[dict]] = {}
    for item in report["items"]:
        key = (item.get("channel_id"), item.get("channel_title") or "")
        grouped.setdefault(key, []).append(item)
    if not grouped:
        lines.append("Открытых исходов нет.")
    for (channel_id, title), items in grouped.items():
        lines.append(str(title or channel_id))
        for item in items:
            label = CATEGORY_LABELS.get(str(item.get("category")), str(item.get("category")))
            link = item.get("post_url") or ""
            head = f"  {label}"
            if link:
                head = f"{head}  {link}"
            lines.append(head)
            reason = item.get("reason")
            if reason:
                lines.append(f"    причина: {reason}")
            if item.get("attempts") is not None:
                lines.append(f"    попытки: {item['attempts']}")
            blocks = "да" if item.get("blocks_cursor") else "нет"
            lines.append(f"    блокирует курсор канала: {blocks}")
    by_category = report["summary"]["by_category"]
    lines.append("")
    lines.append(f"ожидает ревью: {by_category['review']}")
    lines.append(f"ожидает повтора: {by_category['retry']}")
    lines.append(f"ошибка обработки: {by_category['failed']}")
    lines.append(f"пропуск по политике: {by_category['policy']}")
    lines.append(f"всего: {report['summary']['total']}")
    duplicates = report.get("duplicates") or []
    if duplicates:
        lines.append("")
        lines.append("Явные дубли")
        for item in duplicates:
            kind = " (видео)" if item["kind"] == "video" else ""
            lines.append(
                f"  {item['channel_title'] or '?'} {item['post_url'] or item['source_id']}{kind}"
                f" — копия: {item['root_channel_title'] or '?'} {item['root_post_url'] or item['duplicate_of']}"
            )
        lines.append(_duplicates_line(duplicates))
    return "\n".join(lines)


def cmd_ingest_report(
    *,
    channel: str | None = None,
    category: str | None = None,
    as_json: bool = False,
) -> None:
    """Print open ingestion problems. The command does not write."""
    report = build_ingest_report(channel=channel, category=category)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(render_ingest_report(report))


def print_problem_posts_line() -> None:
    """One read-only line at the end of run. A report failure must not fail the run."""
    try:
        report = build_ingest_report()
        print(f"Проблемных постов: {report['summary']['total']}. Подробности: python main.py ingest-report")
        if report["duplicates"]:
            line = _duplicates_line(report["duplicates"])
            print(line[0].upper() + line[1:])
    except Exception:
        logger.exception("Could not build ingest report")


COLLECTORS = [collect_journal_problems, collect_media_problems, collect_enrichment_problems, collect_review_queue_problems]
