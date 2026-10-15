"""Generate a Pyrogram session string for channel-history indexing.

Telegram does not allow *bots* to read channel history, so history backfill
needs a user session. Run this on your own machine (needs API_ID/API_HASH
from https://my.telegram.org -> API development tools), enter the login code
Telegram sends you, then paste the printed SESSION_STRING into the bot's .env.

    python3 tools/make_session.py
"""
import asyncio

from pyrogram import Client


async def main() -> None:
    api_id = int(input("API_ID: ").strip())
    api_hash = input("API_HASH: ").strip()
    async with Client("hist", api_id=api_id, api_hash=api_hash, in_memory=True) as app:
        me = await app.get_me()
        print(f"\nLogged in as @{me.username or me.first_name} ({me.id})")
        print("\nAdd this to your .env:\n")
        print("SESSION_STRING=" + await app.export_session_string())


if __name__ == "__main__":
    asyncio.run(main())
