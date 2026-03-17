"""
generate_session.py
Run this ONCE to get your Telegram StringSession.
Paste the output into TELEGRAM_SESSION in your .env file.
Usage: python generate_session.py
"""
import asyncio, os, sys

def load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip()
            if len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]:
                v = v[1:-1]
            if k:
                os.environ[k] = v

load_env()

API_ID_RAW = os.environ.get("TELEGRAM_API_ID",  "").strip()
API_HASH   = os.environ.get("TELEGRAM_API_HASH", "").strip()
PHONE      = os.environ.get("TELEGRAM_PHONE",    "").strip()

print("\nValues loaded from .env:")
print("  TELEGRAM_API_ID   = '" + API_ID_RAW + "'")
print("  TELEGRAM_API_HASH = '" + (API_HASH[:6] + "****" if API_HASH else "") + "' (" + str(len(API_HASH)) + " chars)")
print("  TELEGRAM_PHONE    = '" + PHONE + "'\n")

errors = []
API_ID = 0
if not API_ID_RAW:
    errors.append("TELEGRAM_API_ID is empty")
else:
    try:
        API_ID = int(API_ID_RAW)
        if API_ID == 0:
            errors.append("TELEGRAM_API_ID is 0 - paste your real numeric ID")
    except ValueError:
        errors.append("TELEGRAM_API_ID must be a number, got: " + API_ID_RAW)

if not API_HASH:
    errors.append("TELEGRAM_API_HASH is empty")
elif len(API_HASH) != 32:
    errors.append("TELEGRAM_API_HASH must be 32 chars, yours is " + str(len(API_HASH)))

if errors:
    print("Fix these issues in your .env file:")
    for e in errors:
        print("  - " + e)
    print("\nCorrect .env format:")
    print("  TELEGRAM_API_ID=12345678")
    print("  TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890")
    print("  TELEGRAM_PHONE=+911234567890")
    print("\nGet credentials at: https://my.telegram.org\n")
    sys.exit(1)

from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    print("Connecting to Telegram...")
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        phone = PHONE or input("Phone number (with country code e.g. +911234567890): ").strip()
        await client.send_code_request(phone)
        code = input("Code Telegram sent to your app/SMS: ").strip()
        try:
            await client.sign_in(phone, code)
        except Exception as e:
            if "SessionPasswordNeeded" in type(e).__name__:
                pw = input("Two-step verification password: ").strip()
                await client.sign_in(password=pw)
            else:
                raise
    me = await client.get_me()
    print("Signed in as: " + str(me.first_name))
    session_str = client.session.save()
    await client.disconnect()
    print("\n" + "="*60)
    print("Paste this line into your .env file:")
    print("="*60)
    print("TELEGRAM_SESSION=" + session_str)
    print("="*60 + "\n")

asyncio.run(main())
