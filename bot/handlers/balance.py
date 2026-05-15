"""/balance — outstanding-balance report + interactive "mark paid" flow.

Read-side
─────────
Lists every person (member OR guest) who currently has unpaid confirmed
signups in this chat, ranked by total owed (descending). For each debtor,
shows the per-game breakdown so it's clear what they owe and for which
games. See module docstring at the top of `_render_balance` for the
exact data-eligibility rules.

Write-side ("Mark paid" flow)
─────────────────────────────
A "💰 Mark paid" button appears below the report when there's at least
one debtor. The flow walks through three callback steps:

  bal_pay         — show debtor picker (snapshot current debtors)
  bal_pick:<idx>  — show per-game toggles for the chosen debtor
                    (default: all games pre-selected)
  bal_toggle:<pid> — flip a single game in the selection set
  bal_confirm     — apply (set is_paid=1 on every selected participant)
                    and re-render the balance with a success header
  bal_back        — back to debtor picker
  bal_cancel      — exit flow and re-render plain balance

Flow state lives in `context.user_data` under keys prefixed `balance_`.
All keys are namespaced so other handlers can't accidentally touch them.

Note: this module owns the `^bal_` callback pattern. It must be
registered BEFORE roster's catch-all CallbackQueryHandler in main.py,
or the catch-all will swallow these.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.helpers import escape

from .. import db, views
from ..chat_picker import resolve_chat, register_command
from .common import touch_member

log = logging.getLogger(__name__)


# ─────────────────────── user_data keys ─────────────────────── #
# Snapshot of debtors taken when "Mark paid" is tapped. Frozen for the
# duration of the flow so debtor indices stay stable across button taps.
_BAL_DEBTORS_KEY = "balance_debtors"
# Index into _BAL_DEBTORS_KEY for the debtor currently being paid for.
_BAL_DEBTOR_IDX_KEY = "balance_debtor_idx"
# Set of participant_ids the user has selected to mark as paid.
_BAL_SELECTED_KEY = "balance_selected_pids"
# The chat_id this flow operates on. Stashed at /balance time so DM
# follow-ups know which group's balances to mutate.
_BAL_CHAT_ID_KEY = "balance_chat_id"


def _clear_pay_state(context: ContextTypes.DEFAULT_TYPE, keep_chat_id: bool = True) -> None:
    """Wipe the in-flight pay flow state. By default keeps chat_id so a
    subsequent re-render of /balance still knows which group to query."""
    for k in (_BAL_DEBTORS_KEY, _BAL_DEBTOR_IDX_KEY, _BAL_SELECTED_KEY):
        context.user_data.pop(k, None)
    if not keep_chat_id:
        context.user_data.pop(_BAL_CHAT_ID_KEY, None)


# ─────────────────────── grouping (read side) ─────────────────────── #

def _group_unpaid_by_debtor(rows: list[dict]) -> list[dict]:
    """Group raw unpaid-signup rows by debtor identity.

    Identity key:
      - Member: ("member", member_id) — display via member_name
      - Guest:  ("guest", lowercased+trimmed guest_name) — case-insensitive
                so "Casey" and "casey" aggregate together. Display name is
                whatever spelling appeared first (earliest game).
    """
    grouped: "OrderedDict[tuple, dict]" = OrderedDict()
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

    return sorted(
        grouped.values(),
        key=lambda d: (-d["total_cents"], d["display_name"].lower()),
    )


def _render_balance(debtors: list[dict], tz: ZoneInfo) -> str:
    """Build the HTML message body for the /balance view."""
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
            host = d["items"][0].get("adder_name") or "?"
            name = f"{name} <i>(guest of {escape(host)})</i>"
        amt = views.format_money(d["total_cents"])
        games_label = "game" if len(d["items"]) == 1 else "games"
        lines.append(
            f"{rank} {name} — <b>{amt}</b> ({len(d['items'])} {games_label})"
        )
        for item in d["items"]:
            when = views.format_when(item["scheduled_for"], tz)
            loc = escape(item["location"])
            line_amt = views.format_money(item["payment_amount_cents"])
            lines.append(f"     • {when} @ {loc} — {line_amt}")
        lines.append("")

    signups_label = "signup" if total_signups == 1 else "signups"
    lines.append(
        f"<i>Total outstanding: {views.format_money(total_outstanding)} "
        f"across {total_signups} unpaid {signups_label}</i>"
    )

    return "\n".join(lines)


def _balance_keyboard(has_debtors: bool) -> Optional[InlineKeyboardMarkup]:
    """The Mark-paid button under the balance view. None when nothing is owed."""
    if not has_debtors:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💰 Mark paid", callback_data="bal_pay"),
    ]])


# ─────────────────────── /balance command ─────────────────────── #

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
    kb = _balance_keyboard(bool(debtors))

    # Stash chat_id so the callback flow knows which group to operate on
    # (matters in DMs where context.chat.id is the DM, not the group).
    context.user_data[_BAL_CHAT_ID_KEY] = chat_id

    # Telegram caps bodies at 4096 chars. Defensive truncation: drop
    # low-rank debtors with a footer note if we'd overflow.
    MAX_LEN = 4000
    if len(text) > MAX_LEN:
        keep = len(debtors)
        while keep > 1:
            keep -= 1
            footer = (
                f"\n\n<i>(+{len(debtors) - keep} more debtor(s) not shown — "
                f"this report hit Telegram's message size limit.)</i>"
            )
            candidate = _render_balance(debtors[:keep], tz) + footer
            if len(candidate) <= MAX_LEN:
                text = candidate
                break

    await update.effective_message.reply_html(text, reply_markup=kb)


def build_balance_handlers() -> list:
    return [CommandHandler("balance", cmd_balance)]


# DM picker re-dispatch hook
register_command("balance", cmd_balance)


# ─────────────────────── pay flow callbacks ─────────────────────── #

async def on_balance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatcher for bal_* callbacks. Routes to the right step handler."""
    from .common import gate
    if not await gate(update):
        return
    await touch_member(update)
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    parts = data.split(":", 1)
    action = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    try:
        if action == "bal_pay":
            await _show_debtor_picker(context, update)
        elif action == "bal_pick":
            try:
                idx = int(arg)
            except ValueError:
                await query.edit_message_text("Picker is stale. Run /balance again.")
                return
            await _show_game_picker_fresh(context, update, idx)
        elif action == "bal_toggle":
            try:
                pid = int(arg)
            except ValueError:
                return
            await _toggle_game(context, update, pid)
        elif action == "bal_confirm":
            await _confirm_payment(context, update)
        elif action == "bal_back":
            await _show_debtor_picker(context, update)
        elif action == "bal_cancel":
            await _exit_to_balance(context, update, success_header=None)
        else:
            log.warning("Unknown balance callback: %s", data)
    except Exception:
        log.exception("Balance callback failed: %s", data)
        try:
            await query.answer("Something went wrong.", show_alert=True)
        except Exception:
            pass


async def _show_debtor_picker(
    context: ContextTypes.DEFAULT_TYPE, update: Update
) -> None:
    """Step 1: show one button per current debtor."""
    query = update.callback_query
    tz: ZoneInfo = context.bot_data["tz"]
    chat_id = context.user_data.get(_BAL_CHAT_ID_KEY)
    if chat_id is None:
        await query.edit_message_text("Picker is stale. Run /balance again.")
        return

    rows = db.list_unpaid_signups(chat_id)
    debtors = _group_unpaid_by_debtor(rows)

    if not debtors:
        # Everyone's paid up — bail back to a plain balance view.
        await _exit_to_balance(context, update, success_header=None)
        return

    # Snapshot for the rest of the flow.
    context.user_data[_BAL_DEBTORS_KEY] = debtors
    # Coming back from a game-picker resets which debtor is being paid for
    # and the selection set, so each pick starts fresh.
    context.user_data.pop(_BAL_DEBTOR_IDX_KEY, None)
    context.user_data.pop(_BAL_SELECTED_KEY, None)

    rows_kb = []
    for i, d in enumerate(debtors):
        label = d["display_name"]
        if d["is_guest"]:
            label += " (guest)"
        label += f" — {views.format_money(d['total_cents'])}"
        # Telegram caps button labels around 64 bytes; keep a short ceiling.
        if len(label) > 50:
            label = label[:47] + "…"
        rows_kb.append([InlineKeyboardButton(label, callback_data=f"bal_pick:{i}")])
    rows_kb.append([InlineKeyboardButton("Cancel", callback_data="bal_cancel")])

    text = (
        "<b>💰 Mark paid</b>\n\n"
        "Who's paying? Tap their name."
    )
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows_kb),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def _show_game_picker_fresh(
    context: ContextTypes.DEFAULT_TYPE, update: Update, debtor_idx: int
) -> None:
    """Step 2 (entry): set the debtor and pre-select all their games."""
    debtors = context.user_data.get(_BAL_DEBTORS_KEY)
    if not debtors or not (0 <= debtor_idx < len(debtors)):
        await update.callback_query.edit_message_text("Picker is stale. Run /balance again.")
        return
    context.user_data[_BAL_DEBTOR_IDX_KEY] = debtor_idx
    # Pre-select everything — the common case is "they paid for all unpaid games"
    context.user_data[_BAL_SELECTED_KEY] = {
        item["participant_id"] for item in debtors[debtor_idx]["items"]
    }
    await _render_game_picker(context, update)


async def _toggle_game(
    context: ContextTypes.DEFAULT_TYPE, update: Update, participant_id: int
) -> None:
    """Step 2 (toggle): flip a single game in the selection set."""
    selected = context.user_data.get(_BAL_SELECTED_KEY)
    if selected is None:
        await update.callback_query.edit_message_text("Picker is stale. Run /balance again.")
        return
    if participant_id in selected:
        selected.remove(participant_id)
    else:
        selected.add(participant_id)
    context.user_data[_BAL_SELECTED_KEY] = selected
    await _render_game_picker(context, update)


async def _render_game_picker(
    context: ContextTypes.DEFAULT_TYPE, update: Update
) -> None:
    """Render the per-debtor game-selection screen using current state."""
    query = update.callback_query
    tz: ZoneInfo = context.bot_data["tz"]
    debtors = context.user_data.get(_BAL_DEBTORS_KEY)
    idx = context.user_data.get(_BAL_DEBTOR_IDX_KEY)
    selected = context.user_data.get(_BAL_SELECTED_KEY, set())

    if not debtors or idx is None or not (0 <= idx < len(debtors)):
        await query.edit_message_text("Picker is stale. Run /balance again.")
        return

    debtor = debtors[idx]
    name = debtor["display_name"]
    if debtor["is_guest"]:
        name += " (guest)"

    rows_kb = []
    for item in debtor["items"]:
        pid = item["participant_id"]
        checked = "✅" if pid in selected else "⬜"
        when = views.format_when_short(item["scheduled_for"], tz)
        loc = item["location"]
        amt = views.format_money(item["payment_amount_cents"])
        label = f"{checked} {when} @ {loc} — {amt}"
        if len(label) > 55:
            label = label[:52] + "…"
        rows_kb.append([InlineKeyboardButton(label, callback_data=f"bal_toggle:{pid}")])

    # Live total of what's currently checked
    selected_total = sum(
        item["payment_amount_cents"]
        for item in debtor["items"]
        if item["participant_id"] in selected
    )
    n_selected = len(selected)
    games_label = "game" if n_selected == 1 else "games"
    if n_selected > 0:
        confirm_label = (
            f"✓ Mark {n_selected} {games_label} paid · "
            f"{views.format_money(selected_total)}"
        )
    else:
        confirm_label = "✓ Mark paid (nothing selected)"
    rows_kb.append([InlineKeyboardButton(confirm_label, callback_data="bal_confirm")])
    rows_kb.append([
        InlineKeyboardButton("← Back", callback_data="bal_back"),
        InlineKeyboardButton("Cancel", callback_data="bal_cancel"),
    ])

    text = (
        f"<b>💰 Mark paid — {escape(name)}</b>\n\n"
        f"Tap games to toggle ✅/⬜. All games are selected by default. "
        f"Then tap <b>Mark paid</b> to apply."
    )
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows_kb),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def _confirm_payment(
    context: ContextTypes.DEFAULT_TYPE, update: Update
) -> None:
    """Step 3: apply set_participant_paid for every selected pid, then
    re-render /balance with a success header."""
    query = update.callback_query
    debtors = context.user_data.get(_BAL_DEBTORS_KEY)
    idx = context.user_data.get(_BAL_DEBTOR_IDX_KEY)
    selected = context.user_data.get(_BAL_SELECTED_KEY)

    if not debtors or idx is None or selected is None or not (0 <= idx < len(debtors)):
        await query.edit_message_text("Picker is stale. Run /balance again.")
        _clear_pay_state(context)
        return

    if not selected:
        # Defend against the "nothing checked" edge case.
        await query.answer("Nothing selected to mark paid.", show_alert=True)
        return

    debtor = debtors[idx]
    paid_count = 0
    paid_total = 0
    for item in debtor["items"]:
        pid = item["participant_id"]
        if pid in selected:
            db.set_participant_paid(pid, True)
            paid_count += 1
            paid_total += item["payment_amount_cents"]

    name = debtor["display_name"]
    if debtor["is_guest"]:
        name += " (guest)"

    success_header = (
        f"<b>✓ Marked {paid_count} game{'s' if paid_count != 1 else ''} "
        f"paid for {escape(name)} — {views.format_money(paid_total)} total.</b>"
    )
    await _exit_to_balance(context, update, success_header=success_header)


async def _exit_to_balance(
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    success_header: Optional[str],
) -> None:
    """Tear down the pay flow and replace the message with a fresh /balance.

    If success_header is set (after a confirm), prepend it so the user
    sees confirmation inline with the updated balances.
    """
    query = update.callback_query
    chat_id = context.user_data.get(_BAL_CHAT_ID_KEY)
    _clear_pay_state(context)

    tz: ZoneInfo = context.bot_data["tz"]
    rows = db.list_unpaid_signups(chat_id) if chat_id is not None else []
    debtors = _group_unpaid_by_debtor(rows)

    body = _render_balance(debtors, tz)
    text = f"{success_header}\n\n{body}" if success_header else body
    kb = _balance_keyboard(bool(debtors))

    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=kb,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def build_balance_callback_handlers() -> list:
    """Pattern-scoped callback handler. MUST be registered before roster's
    catch-all CallbackQueryHandler in main.py."""
    return [CallbackQueryHandler(on_balance_callback, pattern=r"^bal_")]
