"""/spend — per-member spend totals for this calendar year.

Aggregates payment_amount_cents over participants where is_paid=1, ranked
top-to-bottom. Scope: one chat at a time. Window: Jan 1 → Jan 1 of next
year, in the bot's configured timezone. Guests are excluded (no member_id).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes
from telegram.helpers import escape

from .. import db, views
from ..chat_picker import resolve_chat, register_command
from .common import touch_member


def _year_bounds(tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Return (start, end) datetimes covering the current calendar year in `tz`.

    start = Jan 1 00:00 of this year, end = Jan 1 00:00 of next year (exclusive).
    """
    now = datetime.now(tz)
    start = datetime(now.year, 1, 1, tzinfo=tz)
    end = datetime(now.year + 1, 1, 1, tzinfo=tz)
    return start, end


async def cmd_spend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .common import gate
    if not await gate(update):
        return
    await touch_member(update)
    chat_id = await resolve_chat(update, context, "spend")
    if chat_id is None:
        return

    tz: ZoneInfo = context.bot_data["tz"]
    start, end = _year_bounds(tz)
    year_label = start.year

    rows = db.list_member_spend_in_range(chat_id, start, end)

    if not rows:
        await update.effective_message.reply_html(
            f"<b>💸 Spend in {year_label}</b>\n\n"
            f"<i>Nothing paid yet this year. Once people start tapping ✅ Paid "
            f"on game cards, totals will show up here.</i>"
        )
        return

    total_cents = sum(r["total_cents"] for r in rows)
    total_paid_signups = sum(r["games_paid"] for r in rows)

    lines = [f"<b>💸 Spend in {year_label}</b>", ""]
    # Width the rank prefix so the ranks line up nicely (1. vs 10.).
    rank_width = len(str(len(rows)))
    for i, r in enumerate(rows, start=1):
        rank = f"{i}.".ljust(rank_width + 1)
        name = escape(r["display_name"] or "?")
        amt = views.format_money(r["total_cents"])
        games_label = "game" if r["games_paid"] == 1 else "games"
        lines.append(f"{rank} {name} — <b>{amt}</b> ({r['games_paid']} {games_label})")

    lines.append("")
    signups_label = "signup" if total_paid_signups == 1 else "signups"
    lines.append(
        f"<i>Total collected: {views.format_money(total_cents)} "
        f"across {total_paid_signups} paid {signups_label}</i>"
    )

    await update.effective_message.reply_html("\n".join(lines))


def build_spend_handlers() -> list:
    return [CommandHandler("spend", cmd_spend)]


# Register for the DM picker so it re-dispatches after a chat is chosen.
register_command("spend", cmd_spend)
