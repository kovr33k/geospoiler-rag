"""Check channel usernames in GeoSpoiler folder."""
import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from geospoiler.ingestion.fetcher.telegram_client import TelegramFetcher


async def main():
    fetcher = TelegramFetcher()
    await fetcher.connect()
    channels = await fetcher.discover_channels()
    for ch in channels:
        print(f"Title: {ch['title']}")
        print(f"  username: {ch['name']!r}")
        print(f"  id: {ch['id']}")
        username = ch['name']
        msg_id = 5
        if username and not username.lstrip('-').isdigit():
            url = f"https://t.me/{username}/{msg_id}"
        else:
            url = f"https://t.me/c/{ch['id']}/{msg_id}"
        print(f"  example post URL: {url}")
    await fetcher.disconnect()

asyncio.run(main())
