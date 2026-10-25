"""
auth.py — ONE-TIME Telegram authorisation helper.

Run this ONCE in your terminal (NOT via Antigravity):
    python auth.py

It will:
  1. Connect to Telegram
  2. Ask for the verification code you receive in the app
  3. Save the session to state/telegram.session
  4. Exit

After this you can run:
    python main.py fetch 5
    python main.py normalize
"""

import asyncio
import sys

# Force UTF-8 console output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Add project root to path so config imports work

from telethon import TelegramClient

from geospoiler import config


async def main():
    session_path = str(config.STATE_DIR / "telegram")
    print(f"Session will be stored at: {session_path}.session")
    print()

    client = TelegramClient(
        session_path,
        config.TELEGRAM_API_ID,
        config.TELEGRAM_API_HASH,
    )

    await client.start(phone=config.TELEGRAM_PHONE)

    me = await client.get_me()
    print(f"\n[OK] Authorised as: {me.first_name} {me.last_name or ''} (@{me.username})")
    print("Session saved. You can now run: python main.py fetch 5")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
