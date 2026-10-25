"""Read-only Telegram selector for clean China posts.

This script scans the configured Telegram folder, does not update pipeline
state, and does not download media.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from geospoiler import config
from geospoiler.evaluation.model_bakeoff.post_selection import (
    CandidatePost,
    candidate_from_text,
    write_candidate_artifacts,
)
from geospoiler.ingestion.fetcher.telegram_client import TelegramFetcher

DEFAULT_OUTPUT_DIR = config.PROJECT_ROOT / "artifacts" / "model_bakeoff"


async def select_china_posts(
    *,
    limit_per_channel: int = 200,
    max_posts: int = 30,
    channel_keywords: tuple[str, ...] = ("китай", "china", "chn"),
    min_chars: int = 80,
    max_chars: int = 2500,
) -> list[CandidatePost]:
    """Select clean China-related posts from Telegram without mutating progress state."""
    fetcher = TelegramFetcher()
    await fetcher.connect()
    try:
        channels = await fetcher.discover_channels()
        target_channels = [
            channel
            for channel in channels
            if _channel_matches(channel.get("title", ""), channel_keywords)
        ]
        candidates: list[CandidatePost] = []
        for channel in target_channels:
            async for msg in fetcher.client.iter_messages(channel["id"], limit=limit_per_channel):
                if getattr(msg, "media", None) is not None:
                    continue
                text = getattr(msg, "message", "") or ""
                candidate = candidate_from_text(
                    text,
                    post_url=_post_url(channel, getattr(msg, "id", 0)),
                    channel_name=str(channel.get("title", "")),
                    message_id=int(getattr(msg, "id", 0) or 0),
                    date=msg.date.isoformat() if getattr(msg, "date", None) else "",
                    min_chars=min_chars,
                    max_chars=max_chars,
                )
                if candidate is not None:
                    candidates.append(candidate)
        candidates.sort(key=lambda item: (-item.score, item.channel_name, -(item.message_id or 0)))
        return candidates[:max_posts]
    finally:
        await fetcher.disconnect()


def _channel_matches(title: str, keywords: tuple[str, ...]) -> bool:
    lowered = str(title or "").casefold()
    return any(keyword.casefold() in lowered for keyword in keywords)


def _post_url(channel: dict, message_id: int) -> str:
    username = str(channel.get("name") or "")
    if username and not username.lstrip("-").isdigit():
        return f"https://t.me/{username}/{message_id}"
    return f"https://t.me/c/{channel.get('id')}/{message_id}"


def _parse_keywords(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Select clean China Telegram posts for model bakeoff.")
    parser.add_argument("--limit-per-channel", type=int, default=200)
    parser.add_argument("--max-posts", type=int, default=30)
    parser.add_argument("--channel-keywords", default="китай,china,chn")
    parser.add_argument("--min-chars", type=int, default=80)
    parser.add_argument("--max-chars", type=int, default=2500)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    candidates = asyncio.run(
        select_china_posts(
            limit_per_channel=args.limit_per_channel,
            max_posts=args.max_posts,
            channel_keywords=_parse_keywords(args.channel_keywords),
            min_chars=args.min_chars,
            max_chars=args.max_chars,
        )
    )
    jsonl_path, md_path = write_candidate_artifacts(candidates, args.output_dir)
    print(f"Selected China post candidates: {len(candidates)}")
    print(f"JSONL: {jsonl_path}")
    print(f"Markdown: {md_path}")


if __name__ == "__main__":
    main()
