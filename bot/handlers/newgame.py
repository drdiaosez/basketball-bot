"""/newgame — one-shot command that creates a game with default values.

Previously this was a multi-step ConversationHandler that asked for
time, location, max players, payment, and notes one prompt at a time.
That walk-through is gone: `/newgame` now creates a game immediately
with the defaults below and posts the card. Anyone who wants to change
anything taps **⚙ Manage** on the card and edits in the mini-app.

Defaults
────────
  when:        the next Monday at 8:30 PM in the bot's timezone
  duration:    90 minutes (1.5 hours) → card shows "8:30 PM - 10:00 PM"
  location:    "Quartz Sport in Carson"
  max_players: 15
  payment:     none
  notes:       none

`parse_datetime` and `parse_payment` stay in this module — they're
imported by `bot/handlers/roster.py` for the in-chat edit flow that
still serves the old cards already sitting in chat history.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from .. import db
from ..chat_picker import resolve_chat
from .common import touch_member


# ─────────────────────── defaults ─────────────────────── #

DEFAULT_LOCATION = "Quartz Sport in Carson"
DEFAULT_MAX_PLAYERS = 15
DEFAULT_DURATION_MINUTES = 90   # 1.5 hours — card shows 8:30 PM - 10:00 PM
DEFAULT_TIME_WEEKDAY = 0    # Monday (datetime.weekday(): Mon=0 .. Sun=6)
DEFAULT_TIME_HOUR = 20      # 8 PM
DEFAULT_TIME_MINUTE = 30    # :30


def _default_when(tz: ZoneInfo) -> datetime:
    """The *next* Monday at 8:30 PM in `tz` — never today.

    Even if you run /newgame on a Monday morning, the resulting game
    falls on the *following* Monday. We never auto-create a same-day
    card; if you actually want today's game, edit the time via the
    ⚙ Manage mini-app on the posted card.
    """
    now = datetime.now(tz)
    days_ahead = (DEFAULT_TIME_WEEKDAY - now.weekday()) % 7
    if days_ahead == 0:
        # Today is already the target weekday — roll forward a full week
        # so the default is always strictly in the future, regardless of
        # what time of day /newgame was invoked.
        days_ahead = 7
    return (now + timedelta(days=days_ahead)).replace(
        hour=DEFAULT_TIME_HOUR, minute=DEFAULT_TIME_MINUTE, second=0, microsecond=0
    )


def _find_open_game_at(
    chat_id: int, target_dt: datetime, tz: ZoneInfo
) -> dict | None:
    """Open game in this chat scheduled for the same wall-clock minute, or None.

    Match is on the (year, month, day, hour, minute) tuple in the bot's
    timezone — not the date alone. That way an admin who has already
    moved Monday's game to a different time via Manage doesn't block a
    fresh /newgame for the default 8:30 PM slot on the same day.
    """
    target_key = (
        target_dt.year, target_dt.month, target_dt.day,
        target_dt.hour, target_dt.minute,
    )
    for g in db.list_upcoming_games(tz=tz, chat_id=chat_id):
        try:
            dt = datetime.fromisoformat(g["scheduled_for"]).astimezone(tz)
        except ValueError:
            continue
        if (dt.year, dt.month, dt.day, dt.hour, dt.minute) == target_key:
            return g
    return None


# ─────────────────────── /newgame handler ─────────────────────── #

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a game with defaults and post the card.

    In a group chat the card goes in the current chat. In a DM that
    spans multiple groups, resolve_chat() pops a "which group?" picker
    and returns None; the user reruns /newgame after picking. When the
    command was sent in a DM and resolves to a different chat, we
    confirm in the DM so the user knows the card was posted elsewhere.
    """
    from .common import gate
    if not await gate(update):
        return
    await touch_member(update)

    chat_id = await resolve_chat(update, context, "newgame")
    if chat_id is None:
        return  # picker is showing; nothing more to do this turn

    tz: ZoneInfo = context.bot_data["tz"]
    user = update.effective_user
    default_dt = _default_when(tz)

    # Duplicate-prevention: if there's already an open game in this chat
    # scheduled for the same wall-clock minute as the default we'd post,
    # do nothing rather than spamming the group with a near-identical
    # card. Use ⚙ Manage on the existing card to change time/location/etc.
    existing = _find_open_game_at(chat_id, default_dt, tz)
    if existing:
        from .. import views
        when = views.format_when(
            existing["scheduled_for"], tz, existing.get("duration_minutes"),
        )
        await update.effective_message.reply_text(
            f"There's already a game on {when} at {existing['location']}. "
            f"Tap ⚙ Manage on its card to edit it, or use /games to find it."
        )
        return

    game_id = db.create_game(
        scheduled_for=default_dt,
        location=DEFAULT_LOCATION,
        organizer_id=user.id,
        max_players=DEFAULT_MAX_PLAYERS,
        notes=None,
        chat_id=chat_id,
        payment_amount_cents=None,
        duration_minutes=DEFAULT_DURATION_MINUTES,
    )

    # Local import to keep the handlers package's import graph acyclic:
    # roster imports parse_datetime / parse_payment from this module.
    from . import roster
    await roster.post_game_card(context, chat_id, game_id)

    # If the command was issued in a chat OTHER than the game's chat
    # (typical for a DM → group flow), echo a short confirmation back
    # to the issuer so they're not staring at a silent prompt.
    issuing_chat = update.effective_chat
    if issuing_chat and issuing_chat.id != chat_id:
        await update.effective_message.reply_text(
            "✓ Game posted. Tap ⚙ Manage on the card to change time, "
            "location, max players, payment, or notes."
        )


# ─────────────────────── parsers used by roster ─────────────────────── #
# Kept in this module so the in-chat edit flow on old cards still works.

DAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def parse_datetime(text: str, tz: ZoneInfo) -> datetime | None:
    """Parse a forgiving natural-language datetime.

    Accepts things like:
      'wed 6:30pm', 'thursday 7pm', 'tomorrow 6pm', 'today 5:30pm',
      '5/14 6:30pm', '05/20 9:30am', '2026-05-14 18:30'

    Strategy: extract the *date* first (it has stricter syntax — slashes,
    named days, 'today'/'tomorrow'), strip it out, then parse what's left
    as the time. Parsing time-first would mis-grab '05' from '05/20' as
    hour 5.

    Returns timezone-aware datetime in `tz`, or None on failure.
    """
    s = text.strip().lower()
    now = datetime.now(tz)

    # Strict ISO first
    try:
        dt = datetime.fromisoformat(text.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt
    except ValueError:
        pass

    target_date: "date | None" = None
    remainder = s  # what's left after the date is stripped

    # 1. Numeric date like 5/14, 05/20, 5-14, 5/14/2026 — match this FIRST
    #    because it has the most distinctive syntax (slash or dash between digits).
    num_date = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", s)
    if num_date:
        month = int(num_date.group(1))
        day = int(num_date.group(2))
        year = int(num_date.group(3)) if num_date.group(3) else now.year
        if year < 100:
            year += 2000
        try:
            target_date = datetime(year, month, day).date()
        except ValueError:
            return None
        # If no year was given and the date landed in the *distant* past,
        # they probably meant next year (e.g. typing "1/5" in late December).
        # But if it's a recent past date (within 60 days), they almost
        # certainly mistyped or meant a past game — DON'T roll forward to
        # next year. Returning None forces a re-prompt instead of silently
        # creating a game 11 months out.
        if num_date.group(3) is None and target_date < now.date():
            days_behind = (now.date() - target_date).days
            if days_behind > 60:
                # User typed something far in the past (e.g. "1/5" in Dec)
                target_date = datetime(year + 1, month, day).date()
            else:
                # Recent past — refuse the parse, let the user retry
                return None
        remainder = (s[:num_date.start()] + " " + s[num_date.end():]).strip()

    # 2. 'today' / 'tomorrow'
    elif "tomorrow" in s or "tmrw" in s:
        target_date = (now + timedelta(days=1)).date()
        remainder = re.sub(r"\b(tomorrow|tmrw)\b", " ", s).strip()
    elif "today" in s:
        target_date = now.date()
        remainder = re.sub(r"\btoday\b", " ", s).strip()

    # 3. Day of week (mon, tuesday, etc.)
    else:
        for name, weekday in DAY_NAMES.items():
            if re.search(rf"\b{name}\b", s):
                days_ahead = (weekday - now.weekday()) % 7
                if days_ahead == 0:
                    # Same weekday — we'll decide whether to bump to next week
                    # *after* we know the time. Mark as today for now.
                    pass
                target_date = (now + timedelta(days=days_ahead)).date()
                remainder = re.sub(rf"\b{name}\b", " ", s).strip()
                break

    if target_date is None:
        return None

    # Now parse the time from what's left. Accept "6:30pm", "6pm", "18:30",
    # "6:30", "7". Bare numbers without colon or meridiem are still allowed
    # (default AM per group preference).
    time_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", remainder)
    if not time_match:
        return None
    hour = int(time_match.group(1))
    minute = int(time_match.group(2)) if time_match.group(2) else 0
    meridiem = time_match.group(3)

    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    # No meridiem? Leave as-is — this group plays mornings, so a bare
    # "6:30" or "7" means AM. PM games need an explicit "pm".

    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None

    # If the date was a same-weekday match and the resulting time has
    # already passed today, push to next week.
    today_weekday = now.weekday()
    if target_date == now.date() and target_date.weekday() == today_weekday:
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Only push forward if this looked like a "wed" / "tomorrow"-style request,
        # not a numeric date that happens to be today.
        if candidate <= now and not num_date and "today" not in s:
            target_date = (now + timedelta(days=7)).date()

    dt = datetime.combine(target_date, datetime.min.time()).replace(
        hour=hour, minute=minute, tzinfo=tz
    )
    return dt


# Accept "$5", "5", "5.50", "$5.50", "5,00". Reject anything else as bad input.
_PAYMENT_RE = re.compile(r"^\s*\$?\s*(\d+)(?:[.,](\d{1,2}))?\s*$")


def parse_payment(text: str) -> int | None:
    """Parse a money string into cents, or return None for unparseable input.

    Returns 0 for explicit zero ("0", "$0", "0.00") so the caller can
    distinguish that case from a parse failure if it cares.
    """
    m = _PAYMENT_RE.match(text)
    if not m:
        return None
    dollars = int(m.group(1))
    cents_str = m.group(2) or "0"
    # Pad single-digit cents: "5.5" → 50 cents, not 5 cents
    if len(cents_str) == 1:
        cents_str += "0"
    cents = int(cents_str)
    return dollars * 100 + cents


# ─────────────────────── handler registration ─────────────────────── #

def build_newgame_handler() -> CommandHandler:
    """Returned to main.py which registers it with the Application."""
    return CommandHandler("newgame", cmd_newgame)
