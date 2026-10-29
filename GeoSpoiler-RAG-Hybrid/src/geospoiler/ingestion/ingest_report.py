"""Read-only ingest report.

Collectors are independent. Each returns problem items. ``combine_problem_items``
merges them per (channel_id, message_id) and keeps one label, by priority
review → retry → failed → policy. Later stages add a collector and register it
in ``COLLECTORS``.
"""

import copy
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from geospoiler import config
from geospoiler.ingestion.enricher import pipeline as enricher_pipeline
from geospoiler.ingestion.fetcher.state import get_last_message_id
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


def collect_journal_problems() -> list[dict]:
    """Open normalization failures. Other stages register their own collectors."""
    items = []
    for record in read_open_outcomes():
        if record.get("stage") != "normalize" or record.get("outcome") != "failed":
            continue
        items.append(
            {
                "channel_id": record.get("channel_id"),
                "channel_title": record.get("channel_title"),
                "message_id": record.get("message_id"),
                "post_url": record.get("post_url"),
                "stage": record.get("stage"),
                "category": "retry",
                "reason": record.get("reason"),
                "attempts": record.get("attempts"),
                "first_seen": record.get("first_seen"),
                "last_seen": record.get("last_seen"),
            }
        )
    return items


COLLECTORS = [collect_journal_problems]


def combine_problem_items(items: list[dict]) -> list[dict]:
    """Keep one item per channel and message, preferring the higher-priority label."""
    chosen: dict[tuple, dict] = {}
    for item in items:
        key = (item.get("channel_id"), item.get("message_id"))
        current = chosen.get(key)
        if current is None or _CATEGORY_PRIORITY[item["category"]] < _CATEGORY_PRIORITY[current["category"]]:
            chosen[key] = item
    return list(chosen.values())


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _with_cursor_flags(items: list[dict]) -> list[dict]:
    """A post blocks the channel cursor when its message id is still ahead of it."""
    cursors: dict[int, int] = {}
    flagged = []
    for item in items:
        copied = dict(item)
        channel_id = _as_int(copied.get("channel_id"))
        message_id = _as_int(copied.get("message_id"))
        blocks = False
        if channel_id is not None and message_id is not None:
            if channel_id not in cursors:
                cursors[channel_id] = get_last_message_id(channel_id)
            blocks = message_id > cursors[channel_id]
        copied["blocks_cursor"] = blocks
        flagged.append(copied)
    return flagged


def _sort_key(item: dict) -> tuple:
    message_id = item.get("message_id")
    missing_message = message_id is None
    return (
        str(item.get("channel_title") or ""),
        _as_int(item.get("channel_id")) if _as_int(item.get("channel_id")) is not None else 0,
        missing_message,
        message_id if isinstance(message_id, int) and not isinstance(message_id, bool) else 0,
    )


def _summary(items: list[dict]) -> dict:
    by_category = {name: 0 for name in CATEGORIES}
    for item in items:
        category = item.get("category")
        if category in by_category:
            by_category[category] += 1
    return {"total": len(items), "by_category": by_category}


def build_ingest_report(*, channel: str | None = None, category: str | None = None) -> dict:
    """Build the report from registered collectors. This function only reads."""
    collected = []
    for collect in COLLECTORS:
        collected.extend(collect())
    items = _with_cursor_flags(combine_problem_items(collected))
    if channel is not None:
        items = [item for item in items if item.get("channel_title") == channel]
    if category is not None:
        items = [item for item in items if item.get("category") == category]
    items.sort(key=_sort_key)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": _summary(items),
        "items": items,
    }


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
        total = build_ingest_report()["summary"]["total"]
        print(f"Проблемных постов: {total}. Подробности: python main.py ingest-report")
    except Exception:
        logger.exception("Could not build ingest report")


def _read_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _coerce_id(value: object) -> int | str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.lstrip("-").isdigit():
            return int(text)
        return text
    return None


def _text_or_none(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _meta_for_review(item: dict) -> dict:
    """Normalized metadata for a review item. Review files store the channel name, not its id."""
    candidates: list[Path] = []
    raw_path = item.get("normalized_filepath")
    if isinstance(raw_path, str) and raw_path.strip():
        text_path = Path(raw_path)
        if text_path.name.endswith(".meta.json"):
            candidates.append(text_path)
        else:
            candidates.append(text_path.with_suffix(".meta.json"))
    channel = item.get("channel")
    message_id = item.get("message_id")
    if isinstance(channel, str) and channel.strip() and message_id is not None:
        filename = f"{message_id}.meta.json"
        directory = _sanitize(channel)
        candidates.append(config.NORMALIZED_DIR / directory / filename)
        if directory != channel:
            candidates.append(config.NORMALIZED_DIR / channel / filename)
    for path in candidates:
        meta = _read_json_object(path)
        if meta:
            return meta
    return {}


def _review_reason(item: dict) -> str | None:
    review_type = str(item.get("review_type") or "").strip()
    detail = str(item.get("reason") or "").strip() or str(item.get("url") or "").strip()
    if review_type and detail:
        return f"{review_type}: {detail}"
    return review_type or detail or None


def _problem_item(
    *,
    channel_id: int | str | None,
    channel_title: str | None,
    message_id: int | str | None,
    post_url: str | None,
    stage: str,
    category: str,
    reason: str | None,
) -> dict:
    return {
        "channel_id": channel_id,
        "channel_title": channel_title,
        "message_id": message_id,
        "post_url": post_url,
        "stage": stage,
        "category": category,
        "reason": reason,
        "attempts": None,
        "first_seen": None,
        "last_seen": None,
    }


def collect_enrichment_problems() -> list[dict]:
    """Posts whose normalized material has no up-to-date Enriched v2 card."""
    if not config.NORMALIZED_DIR.is_dir():
        return []
    progress = copy.deepcopy(enricher_pipeline._load_progress())
    stats = enricher_pipeline.EnrichmentStats()
    jobs = enricher_pipeline._collect_enrichment_jobs(
        progress=progress,
        channel_filter=None,
        force=False,
        stats=stats,
        read_only=True,
    )
    items = []
    for job in jobs:
        meta = _read_json_object(job.meta_path)
        items.append(
            _problem_item(
                channel_id=_coerce_id(meta.get("channel_id")),
                channel_title=_text_or_none(meta.get("channel_name")) or job.channel_name,
                message_id=_coerce_id(job.msg_id),
                post_url=_text_or_none(meta.get("post_url")),
                stage="enrich",
                category="retry",
                reason="нет актуальной карточки",
            )
        )
    return items


def collect_review_queue_problems() -> list[dict]:
    """Pending review items. Enrichment failures stay retry through enricher selection."""
    items = []
    for review in get_pending_reviews():
        if review.get("review_type") == REVIEW_TYPE_ENRICHMENT_FAILURE:
            continue
        meta = _meta_for_review(review)
        items.append(
            _problem_item(
                channel_id=_coerce_id(meta.get("channel_id")),
                channel_title=_text_or_none(meta.get("channel_name")) or _text_or_none(review.get("channel")),
                message_id=_coerce_id(review.get("message_id")),
                post_url=_text_or_none(meta.get("post_url")),
                stage="review",
                category="review",
                reason=_review_reason(review),
            )
        )
    return items


COLLECTORS.append(collect_enrichment_problems)
COLLECTORS.append(collect_review_queue_problems)
