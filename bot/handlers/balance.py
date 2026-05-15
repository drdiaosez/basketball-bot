"""/balance — outstanding-balance report.

Lists every person (member OR guest) who currently has unpaid confirmed
signups in this chat, ranked by total owed (descending). For each debtor,
shows the per-game breakdown so it's clear what they owe and for which
games.

Definition of "owes money":
  - Confirmed on a game (maybes / waitlist don't owe)
  - is_paid = 0
  - Game has a payment_amount_cents > 0
  - Game isn't cancelled
  - Game is in the current chat

No time filter — past and upcoming unpaid games both count, since the
debt is real either way.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes
from telegram.helpers import escape

from .. import db, views
from ..chat_picker import resolve_chat, register_command
from .common import touch_member


def _group_unpaid_by_debtor(rows: list[dict]) -> list[dict]:
    """Group raw unpaid-signup rows by debtor identity.

    Identity key:
      - Member: ("member", member_id) — display via member_name
      - Guest:  ("guest", lowercased+trimmed guest_name) — case-insensitive
                so "Casey" and "casey" aggregate together. Display name is
                whatever spelling appeared first (earliest game).

    Returns a list of debtor dicts ordered by total_cents DESC, then
    display_name ASC (case-insensitive). Each entry has:
      {
        "key":          tuple,   # internal grouping key
        "display_name": str,     # name to render (member name or first guest spelling)
        "is_guest":     bool,
        "total_cents":  int,
        "items":        list[dict],  # original rows that contributed, in date order
      }
    """
    grouped: "OrderedDict[tuple, dict]" = OrderedDict()
    # rows arrive ordered by scheduled_for ASC (see db.list_unpaid_signups),
    # so the first encounter of a guest name gives us the canonical spelling.
    for r in rows:
        if r["member_id"] is not None:
            key = ("member", r["member_id"])
            display = r["member_name"] or "?"
            is_guest = False
        else:
            raw_name = (r["guest_name"] or "").strip()
            key = ("guest", raw_name.lower())
            display = raw_name or "?"
            is_guest = True

        if key not in grouped:
            grouped[key] = {
                "key": key,
                "display_name": display,
                "is_guest": is_guest,
                "total_cents": 0,
                "items": [],
            }
        grouped[key]["total_cents"] += r["payment_amount_cents"]
        grouped[key]["items"].append(r)

    # Sort: total desc, then display_name asc (case-insensitive).
    return sorted(
        grouped.values(),
        key=lambda d: (-d["total_cents"], d["display_name"].lower()),
    )


def _render_balance(debtors: list[dict], tz: ZoneInfo) -> str:
    """Build the HTML message body. Caller is responsible for sending it."""
    if not debtors:
        return (
            "<b>💰 Outstanding balances</b>\n\n"
            "<i>Everyone's paid up. 🎉</i>\n\n"
            "<i>(This lists confirmed players with ✅ Paid unchecked on games "
            "that have a per-person amount set.)</i>"
        )

    total_outstanding = sum(d["total_cents"] for d in debtors)
    total_signups = sum(len(d["items"]) for d in debtors)

    lines = ["<b>💰 Outstanding balances</b>", ""]
    rank_width = len(str(len(debtors)))

    for i, d in enumerate(debtors, start=1):
        rank = f"{i}.".ljust(rank_width + 1)
        name = escape(d["display_name"])
        if d["is_guest"]:
            # Annotate guests so it's obvious who's who. Use the adder's name
            # from the first contributing row as the "host" label.
            host = d["items"][0].get("adder_name") or "?"
            name = f"{name} <i>(guest of {escape(host)})</i>"
        amt = views.format_money(d["total_cents"])
        games_label = "game" if len(d["items"]) == 1 else "games"
        lines.append(
            f"{rank} {name} — <b>{amt}</b> ({len(d['items'])} {games_label})"
        )
        # Per-game breakdown. Order = earliest first (already sorted in the
        # DB query). Each line: "    • Wed May 14 · 6:30 PM @ <location> — $5"
        for item in d["items"]:
            when = views.format_when(item["scheduled_for"], tz)
            loc = escape(item["location"])
            line_amt = views.format_money(item["payment_amount_cents"])
            lines.append(f"     • {when} @ {loc} — {line_amt}")
        lines.append("")  # spacer between debtors

    signups_label = "signup" if total_signups == 1 else "signups"
    lines.append(
        f"<i>Total outstanding: {views.format_money(total_outstanding)} "
        f"across {total_signups} unpaid {signups_label}</i>"
    )

    return "\n".join(lines)


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .common import gate
    if not await gate(update):
        return
    await touch_member(update)
    chat_id = await resolve_chat(update, context, "balance")
    if chat_id is None:
        return

    tz: ZoneInfo = context.bot_data["tz"]
    rows = db.list_unpaid_signups(chat_id)
    debtors = _group_unpaid_by_debtor(rows)
    text = _render_balance(debtors, tz)

    # Telegram caps message bodies at 4096 chars. For a normal pickup group
    # we're nowhere close, but if it overflows we'd rather send a truncated
    # report than crash. Trim from the bottom (least-owing folks dropped first)
    # if needed; the totals footer still reflects the FULL outstanding amount.
    MAX_LEN = 4000  # leave headroom for the truncation note
    if len(text) > MAX_LEN:
        # Re-render with progressively fewer debtors until it fits.
        keep = len(debtors)
        while keep > 1:
            keep -= 1
            trimmed = debtors[:keep]
            footer = (
                f"\n\n<i>(+{len(debtors) - keep} more debtor(s) not shown — "
                f"this report hit Telegram's message size limit.)</i>"
            )
            candidate = _render_balance(trimmed, tz) + footer
            if len(candidate) <= MAX_LEN:
                text = candidate
                break

    await update.effective_message.reply_html(text)


def build_balance_handlers() -> list:
    return [CommandHandler("balance", cmd_balance)]


# DM picker re-dispatch hook
register_command("balance", cmd_balance)
