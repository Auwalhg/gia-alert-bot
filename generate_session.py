from getpass import getpass
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

print("=== GIA Telegram Session Generator ===")
print("Kada ka raba TG_SESSION da kowa.\n")
api_id = int(input("Shigar da TG_API_ID: ").strip())
api_hash = getpass("Shigar da TG_API_HASH: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as client:
    session_string = client.session.save()
print("\nTG_SESSION naka:\n")
print(session_string)
print("\nKa adana shi a hosting Variables/Secrets. Kada ka saka shi a GitHub.")
