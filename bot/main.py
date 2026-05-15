"""Entry point.

Run with:  python -m bot.main

Reads from .env:
  BOT_TOKEN         — required (from BotFather)
  DB_PATH           — defaults to db.sqlite
  TIMEZONE          — IANA name, defaults to America/Los_Angeles
  ALLOWED_GROUP_ID  — optional; locks bot to one group (legacy)
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler

from . import db, chat_picker
from .handlers import balance, common, games, newgame, roster, spend, chat_events


async def amain() -> None:
    load_dotenv()
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger(__name__)

    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is required (put it in .env)")
    db_path = os.environ.get("DB_PATH", "db.sqlite")
    tz_name = os.environ.get("TIMEZONE", "America/Los_Angeles")

    db.init_db(db_path)

    app = Application.builder().token(token).build()
    app.bot_data["tz"] = ZoneInfo(tz_name)

    # /start, /help
    app.add_handler(CommandHandler("start", common.cmd_start))
    app.add_handler(CommandHandler("help", common.cmd_help))

    # /newgame conversation — must register before generic text handler
    app.add_handler(newgame.build_newgame_handler())

    # /games, /mygames, /past, /week
    for h in games.build_games_handlers():
        app.add_handler(h)

    # /spend — per-member spend totals for the calendar year
    for h in spend.build_spend_handlers():
        app.add_handler(h)

    # /balance — outstanding-balance report (members + guests)
    for h in balance.build_balance_handlers():
        app.add_handler(h)

    # /cancelguest, /canceledit (must come before generic text handler)
    app.add_handler(roster.build_cancel_guest_handler())
    app.add_handler(roster.build_cancel_edit_handler())

    # Chat-picker callback must be registered BEFORE roster's catch-all
    # CallbackQueryHandler — roster has no pattern filter so it would swallow
    # pick_chat:* callbacks otherwise. register_command() calls at module
    # import time have already populated COMMAND_REGISTRY by this point.
    for h in chat_picker.build_picker_handlers():
        app.add_handler(h)

    # Roster callbacks (catch-all) + text dispatcher — must come AFTER the
    # picker handler above.
    for h in roster.build_roster_handlers():
        app.add_handler(h)

    # my_chat_member / chat_member events — register AFTER command handlers,
    # since these are non-command updates and won't conflict.
    for h in chat_events.build_chat_event_handlers():
        app.add_handler(h)

    app.add_error_handler(common.error_handler)

    # ─── Boot ───
    log.info("Bot starting…")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=["message", "callback_query", "my_chat_member", "chat_member"]
    )
    log.info("Telegram polling started")

    # Block until a signal, then shut down cleanly
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows
    await stop.wait()

    log.info("Shutting down…")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
