"""Read-only ingest report.

Collectors are independent. Each returns problem items. ``combine_problem_items``
merges them per (channel_id, message_id) and keeps one label, by priority
review → retry → failed → policy. Later stages add a collector and register it
in ``COLLECTORS``.
"""

import json
import logging
from datetime import UTC, datetime

from geospoiler.ingestion.fetcher.state import get_last_message_id
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
