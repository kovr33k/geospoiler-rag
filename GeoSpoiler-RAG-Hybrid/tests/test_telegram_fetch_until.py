import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from geospoiler import config
from geospoiler.ingestion.fetcher import telegram_client
from geospoiler.ingestion.fetcher.telegram_client import TelegramFetcher


def _fetch_ids(monkeypatch, until):
    posts = [
        SimpleNamespace(id=1, date=datetime(2026, 2, 6, 12, tzinfo=UTC), grouped_id=None),
        SimpleNamespace(id=2, date=datetime(2026, 2, 7, 23, 59, tzinfo=UTC), grouped_id=None),
        SimpleNamespace(id=3, date=datetime(2026, 2, 8, 0, 1, tzinfo=UTC), grouped_id=None),
    ]

    async def iterate(entity, min_id=0, limit=None, reverse=False):
        for post in posts:
            yield post

    async def process(channel, group):
        return group[0].id

    monkeypatch.setattr(config, "TELEGRAM_FETCH_UNTIL", until)
    monkeypatch.setattr(telegram_client, "get_last_message_id", lambda channel_id: 0)
    fetcher = TelegramFetcher.__new__(TelegramFetcher)
    fetcher.client = SimpleNamespace(iter_messages=iterate)
    fetcher._process_message_group = process
    return asyncio.run(fetcher.fetch_channel_messages({"title": "c", "id": 1}))


def test_fetch_stops_after_the_inclusive_until_date(monkeypatch):
    assert _fetch_ids(monkeypatch, "2026-02-07") == [1, 2]


def test_fetch_stops_after_an_exact_until_datetime(monkeypatch):
    assert _fetch_ids(monkeypatch, "2026-02-07T23:59:00+00:00") == [1, 2]
    assert _fetch_ids(monkeypatch, "2026-02-07T23:58:59") == [1]


def test_fetch_without_until_date_reads_everything(monkeypatch):
    assert _fetch_ids(monkeypatch, "") == [1, 2, 3]
