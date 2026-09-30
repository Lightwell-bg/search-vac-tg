"""Interactive Telethon login; creates the session file (a secret!).

Local:  python scripts/telegram_login.py
Docker: docker compose run --rm search-vac-tg python scripts/telegram_login.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio  # noqa: E402
import getpass  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402

from telethon import TelegramClient  # noqa: E402

from src.config import load_settings  # noqa: E402


def _ask_phone() -> str:
    """Phone number in international format; asks again until it has digits only."""
    while True:
        raw = input("Номер телефона аккаунта в международном формате (например +79161234567): ")
        digits = re.sub(r"[+()\s\-]", "", raw)
        if digits.isdigit() and 10 <= len(digits) <= 15:
            return "+" + digits
        print("Не похоже на номер. Введите только цифры с кодом страны, например +79161234567.")


async def main() -> int:
    s = load_settings()
    if s.telegram_api_id is None or not s.telegram_api_hash:
        print("Fill TELEGRAM_API_ID and TELEGRAM_API_HASH in .env (https://my.telegram.org)")
        return 2
    s.session_file.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(s.session_file), s.telegram_api_id, s.telegram_api_hash)
    try:
        # Telethon's own login flow: re-asks on an invalid phone or a wrong code, asks
        # for the 2FA password when the account has one
        await client.start(
            phone=_ask_phone,
            code_callback=lambda: input("Код из Telegram (придёт в чат «Telegram»): ").strip(),
            password=lambda: getpass.getpass("Пароль двухэтапной проверки (2FA): "),
            max_attempts=5,
        )
        me = await client.get_me()
        print(f"OK: logged in as {me.first_name} ({me.id})")
        print(f"Session file: {s.session_file}.session - treat it as a secret, never commit or share it.")
    finally:
        await client.disconnect()
    for target, mode in ((Path(str(s.session_file) + ".session"), 0o600), (s.session_file.parent, 0o700)):
        try:
            os.chmod(target, mode)  # best effort; ignored on Windows
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
