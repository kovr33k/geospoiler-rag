import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


from telethon import TelegramClient

from geospoiler import config


async def check():
    session_path = str(config.STATE_DIR / 'telegram')
    client = TelegramClient(session_path, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    await client.connect()
    auth = await client.is_user_authorized()
    if auth:
        me = await client.get_me()
        print(f"AUTHORIZED AS: {me.first_name} (@{me.username})")
    else:
        print("NOT_AUTHORIZED")
    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(check())
