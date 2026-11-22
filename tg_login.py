#!/usr/bin/env python3
"""One-time login of YOUR Telegram account for the OLX auto-posting feature.

Run in a normal Terminal window:   python3 ~/bazos-bot/tg_login.py
You type api_id / api_hash (from https://my.telegram.org -> API development tools),
your phone number, the code Telegram sends you (and the 2FA password if you have one).
Nothing is printed or sent anywhere except Telegram and your own GitHub repo secrets.
"""
import getpass
import subprocess
import sys

try:
    from telethon.sync import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    sys.exit("Сначала выполни: python3 -m pip install --user telethon")

import re

api_id_s = input("api_id (только цифры): ").strip()
api_hash = input("api_hash (32 символа 0-9 a-f): ").strip()
if not api_id_s.isdigit():
    sys.exit("api_id должен состоять только из цифр (например 1234567). Проверь на my.telegram.org.")
if not re.fullmatch(r"[0-9a-f]{32}", api_hash):
    sys.exit(f"api_hash должен быть ровно 32 символа из 0-9 и a-f, у тебя {len(api_hash)}. Скопируй его заново, без пробелов.")
api_id = int(api_id_s)
with TelegramClient(StringSession(), api_id, api_hash) as client:  # asks phone, code, 2FA in the terminal
    session = client.session.save()
    me = client.get_me()
print(f"\nВошли как: {me.first_name}. Сохраняю секреты в GitHub…")
for name, val in (("TG_API_ID", str(api_id)), ("TG_API_HASH", api_hash), ("TG_SESSION", session)):
    subprocess.run(["gh", "secret", "set", name, "--repo", "vladyslavbaranets157-cyber/bazos-bot"],
                   input=val.encode(), check=True)
print("Готово: секреты сохранены. Можно возвращаться к Claude и написать «готово».")
