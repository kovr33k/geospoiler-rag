import asyncio
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


import qrcode
from telethon import TelegramClient

from geospoiler import config


async def main():
    session_path = str(config.STATE_DIR / "telegram")
    print(f"Подключение к Telegram (сессия: {session_path}.session)...")

    client = TelegramClient(
        session_path,
        config.TELEGRAM_API_ID,
        config.TELEGRAM_API_HASH,
    )

    await client.connect()

    while not await client.is_user_authorized():
        try:
            qr_login = await client.qr_login()

            # Print QR directly to terminal
            qr = qrcode.QRCode(version=1, box_size=1, border=2)
            qr.add_data(qr_login.url)
            qr.make(fit=True)

            print("\n" + "="*50)
            print("ОТСКАНИРУЙТЕ ЭТОТ QR-КОД ИЗ ПРИЛОЖЕНИЯ TELEGRAM:")
            print("Тел: Настройки -> Устройства -> Подключить устройство")
            print("="*50 + "\n")

            # Print ASCII QR
            qr.print_ascii(invert=True)

            print("\n" + "="*50)
            print("Ожидаю сканирования... (у вас есть 30 секунд)")

            # Save a very clear image as backup
            qr_img = qrcode.QRCode(version=1, box_size=20, border=5)
            qr_img.add_data(qr_login.url)
            qr_img.make(fit=True)
            img = qr_img.make_image(fill_color="black", back_color="white")
            img_path = str(config.PROJECT_ROOT / "artifacts" / "telegram_qr_big.png")
            os.makedirs(os.path.dirname(img_path), exist_ok=True)
            img.save(img_path)

            print("Если в консоли считывается плохо, откройте этот файл:")
            print(img_path)

            await qr_login.wait(timeout=30)
            print("\nУРА! Вы успешно авторизовались.")
            break
        except Exception:
            print("\n[!] Время действия QR-кода вышло. Генерирую свежий...")
            await asyncio.sleep(1)

    me = await client.get_me()
    if me:
        print(f"\n[OK] Аккаунт подтвержден: {me.first_name} (@{me.username})")
        print("Теперь вы можете запускать пайплайн (run_pipeline.ps1)!")

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
