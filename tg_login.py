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

api_id = int(input("api_id: ").strip())
api_hash = getpass.getpass("api_hash (ввод скрыт): ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as client:  # asks phone, code, 2FA in the terminal
    session = client.session.save()
    me = client.get_me()
print(f"\nВошли как: {me.first_name}. Сохраняю секреты в GitHub…")
for name, val in (("TG_API_ID", str(api_id)), ("TG_API_HASH", api_hash), ("TG_SESSION", session)):
    subprocess.run(["gh", "secret", "set", name, "--repo", "vladyslavbaranets157-cyber/bazos-bot"],
                   input=val.encode(), check=True)
print("Готово: секреты сохранены. Можно возвращаться к Claude и написать «готово».")
