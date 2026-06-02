"""HTTP server that runs alongside the Telegram bot.

The basketball bot previously had no HTTP surface. We're adding one so
the game card can host two Telegram Mini Apps:

  • /register — every chat member can self-serve their signup status
    (join / leave / maybe / confirm), plus add and remove their own guests.
  • /manage   — chat admins can edit anyone's signup, change game details,
    or delete the game.

Authentication model
────────────────────
All /api/* requests carry Telegram's signed `initData` blob in the
X-Telegram-Init-Data header. We verify the HMAC against BOT_TOKEN, then
trust the embedded user_id. The user must also be a member of the chat
the game belongs to. Admin endpoints additionally require the user to be
a current chat admin (cached via bot.admins).

After every state change, we re-render the game card in the original
group chat so signups, edits, and deletions are immediately visible to
everyone — same UX as the old in-chat button flow, just driven from the
mini-app instead.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from aiohttp import web
from telegram import Bot

from . import db, views, admins, notifications

log = logging.getLogger(__name__)

# Verified initData cache: hash → (user_id, expiry_ts). The Mini App
# typically fires several API calls in quick succession after launch;
# verifying the HMAC once and caching for a few minutes shaves real
# wall-clock latency without sacrificing security (auth_date in
# initData already bounds freshness).
_AUTH_CACHE: dict[str, tuple[int, float]] = {}
_AUTH_CACHE_TTL_S = 300


# ─────────────────────────────────────────────
# initData verification
# ─────────────────────────────────────────────

def verify_init_data(init_data: str, bot_token: str) -> Optional[int]:
    """Verify a Telegram WebApp initData blob and return the user ID if valid.

    Spec: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data:
        return None

    cache_key = hashlib.sha256(init_data.encode()).hexdigest()
    if cache_key in _AUTH_CACHE:
        user_id, expiry = _AUTH_CACHE[cache_key]
        if expiry > time.time():
            return user_id

    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None

    # Reject initData older than 24 hours per Telegram's recommendation.
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date < time.time() - 86400:
        return None

    try:
        user = json.loads(pairs.get("user", "{}"))
        user_id = int(user["id"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None

    _AUTH_CACHE[cache_key] = (user_id, time.time() + _AUTH_CACHE_TTL_S)
    return user_id


# ─────────────────────────────────────────────
# Auth middleware
# ─────────────────────────────────────────────

@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Apply auth to /api/* routes. Static and health routes pass through."""
    if not request.path.startswith("/api/"):
        return await handler(request)

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    bot_token = request.app["bot_token"]
    user_id = verify_init_data(init_data, bot_token)

    if user_id is None:
        # Optional dev bypass — same pattern as pickleball-bot.
        dev_user = os.environ.get("DEV_BYPASS_USER_ID")
        if dev_user and request.headers.get("X-Dev-Bypass") == os.environ.get(
            "DEV_BYPASS_SECRET", "no"
        ):
            user_id = int(dev_user)
        else:
            return web.json_response({"error": "unauthorized"}, status=401)

    # Must be a known member of the bot at all.
    if db.get_member(user_id) is None:
        return web.json_response({"error": "not a member"}, status=403)

    # For game-scoped routes, require the user to be a member of the
    # specific chat that owns the game. This prevents cross-chat data
    # leaks (group A's admin can't peek at group B's game).
    game_id_str = request.match_info.get("game_id")
    if game_id_str is not None:
        try:
            game_id = int(game_id_str)
        except ValueError:
            return web.json_response({"error": "bad id"}, status=400)
        game = db.get_game(game_id)
        if game is None:
            return web.json_response({"error": "game not found"}, status=404)
        chat_id = game.get("chat_id")
        if chat_id is not None:
            member_row = db.get_chat_member(chat_id, user_id)
            if member_row is None:
                return web.json_response({"error": "not a member of this chat"}, status=403)
        request["game"] = game
        request["chat_id"] = chat_id

    request["user_id"] = user_id
    return await handler(request)


async def _require_admin(request: web.Request) -> Optional[web.Response]:
    """Helper for admin-only endpoints. Returns a Response on failure, or None on success."""
    bot: Bot = request.app["bot"]
    chat_id = request.get("chat_id")
    user_id = request["user_id"]
    if chat_id is None:
        return web.json_response({"error": "game has no chat"}, status=400)
    if not await admins.is_chat_admin(bot, chat_id, user_id):
        return web.json_response({"error": "admin only"}, status=403)
    return None


# ─────────────────────────────────────────────
# Static / health routes
# ─────────────────────────────────────────────

async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


def _serve_static(filename: str) -> Callable[[web.Request], Awaitable[web.Response]]:
    """Build a handler that serves a single file from bot/static/."""
    static_dir = Path(__file__).parent / "static"
    path = static_dir / filename

    async def _handler(request: web.Request) -> web.Response:
        if not path.exists():
            return web.Response(text=f"{filename} not deployed", status=500)
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    return _handler


# ─────────────────────────────────────────────
# Game state serializer
# ─────────────────────────────────────────────

def _participant_dict(p: dict) -> dict:
    """Slim a participant row down to what the mini-app actually needs."""
    if p.get("member_id") is not None:
        name = p.get("member_name") or "Unknown"
        kind = "member"
    else:
        name = p.get("guest_name") or "Guest"
        kind = "guest"
    return {
        "id": p["id"],
        "kind": kind,
        "name": name,
        "member_id": p.get("member_id"),
        "added_by": p.get("added_by"),
        "adder_name": p.get("adder_name"),
        "status": p["status"],
        "position": p["position"],
        "is_paid": bool(p.get("is_paid")),
    }


async def _build_game_state(
    request: web.Request, *, include_admin_extras: Optional[bool] = None
) -> dict:
    """Assemble the JSON payload both mini-apps consume."""
    game: dict = request["game"]
    user_id: int = request["user_id"]
    chat_id = request.get("chat_id")
    tz: ZoneInfo = request.app["tz"]

    bot: Bot = request.app["bot"]
    is_admin = False
    if chat_id is not None:
        is_admin = await admins.is_chat_admin(bot, chat_id, user_id)

    participants = db.get_participants(game["id"])
    organizer = db.get_member(game["organizer_id"])
    organizer_name = organizer["display_name"] if organizer else "?"

    # Viewer's own self-registration (if any)
    own = next((p for p in participants if p.get("member_id") == user_id), None)
    # Viewer's guests
    my_guests = [
        p for p in participants
        if p.get("added_by") == user_id and p.get("member_id") is None
    ]

    confirmed = [p for p in participants if p["status"] == "confirmed"]
    maybe = [p for p in participants if p["status"] == "maybe"]
    waitlist = [p for p in participants if p["status"] == "waitlist"]
    paid_count = sum(1 for p in confirmed if p.get("is_paid"))

    payload = {
        "game": {
            "id": game["id"],
            "scheduled_for": game["scheduled_for"],
            "scheduled_for_label": views.format_when(
                game["scheduled_for"], tz, game.get("duration_minutes"),
            ),
            "duration_minutes": game.get("duration_minutes"),
            "location": game["location"],
            "organizer_name": organizer_name,
            "max_players": game["max_players"],
            "notes": game.get("notes") or "",
            "payment_amount_cents": game.get("payment_amount_cents"),
            "payment_amount_label": views.format_money(game.get("payment_amount_cents")),
            "has_payment": views.game_has_payment(game),
        },
        "viewer": {
            "user_id": user_id,
            "is_admin": is_admin,
            "registration": _participant_dict(own) if own else None,
            "guests": [_participant_dict(g) for g in my_guests],
        },
        "counts": {
            "confirmed": len(confirmed),
            "maybe": len(maybe),
            "waitlist": len(waitlist),
            "paid": paid_count,
        },
        "participants": {
            "confirmed": [_participant_dict(p) for p in confirmed],
            "maybe": [_participant_dict(p) for p in maybe],
            "waitlist": [_participant_dict(p) for p in waitlist],
        },
    }

    # The admin mini-app needs the picker of members-not-yet-on-the-game.
    # Default to "include only if viewer is admin" so non-admin payloads
    # stay slim; callers can override with the kwarg.
    if include_admin_extras is None:
        include_admin_extras = is_admin
    if include_admin_extras and chat_id is not None:
        payload["available_members"] = [
            {"telegram_id": m["telegram_id"], "display_name": m["display_name"]}
            for m in db.list_members_not_in_game(game["id"], chat_id=chat_id)
        ]

    return payload


# ─────────────────────────────────────────────
# Card refresher
# ─────────────────────────────────────────────

async def _notify_diffs(
    request: web.Request, game_id: int, before: list[dict], actor_id: int
) -> None:
    """Diff `before` against current state and DM affected members.

    Keeps the call sites tidy — each mutating endpoint does
    `before = db.get_participants(...)` upfront, then calls this after
    the mutation. Errors are swallowed: DM failures must never break a
    user-facing API call.
    """
    try:
        after = db.get_participants(game_id)
        await notifications.diff_and_notify(
            bot=request.app["bot"],
            tz=request.app["tz"],
            game_id=game_id,
            before=before,
            after=after,
            actor_id=actor_id,
        )
    except Exception as e:
        log.warning("diff_and_notify failed for game %s: %s", game_id, e)


async def _refresh_card(request: web.Request, game_id: int) -> None:
    """Re-render the game card in the group after a mutating API call.

    Telegram doesn't let us push updates to clients without going through
    the bot, so the canonical "everyone sees fresh state" path is:
    edit the original card message in the group. The mini-app polls our
    GET endpoint on its own to reflect the same change in-app.

    We swallow exceptions here — the API write already succeeded; failing
    to refresh the card is annoying but not a data-correctness issue, and
    we don't want to fail the user's tap because Telegram rate-limited us.
    """
    refresher = request.app.get("refresh_card")
    if refresher is None:
        return
    try:
        await refresher(game_id)
    except Exception as e:
        log.warning("card refresh failed for game %s: %s", game_id, e)


# ─────────────────────────────────────────────
# Member-facing routes
# ─────────────────────────────────────────────

async def get_game_state(request: web.Request) -> web.Response:
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def post_self_register(request: web.Request) -> web.Response:
    """Set the viewer's own registration.

    body: {"action": "join" | "maybe" | "confirm" | "leave"}

    "join"    → become confirmed (or waitlist if full)
    "maybe"   → become maybe (or stay there)
    "confirm" → only valid if currently maybe; promote to confirmed/waitlist
    "leave"   → remove yourself entirely
    """
    game: dict = request["game"]
    user_id: int = request["user_id"]
    try:
        body = await request.json()
        action = str(body.get("action") or "").lower()
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"error": "bad json"}, status=400)

    if action not in ("join", "maybe", "confirm", "leave"):
        return web.json_response({"error": "bad action"}, status=400)

    existing = db.member_is_in_game(game["id"], user_id)
    before = db.get_participants(game["id"])

    try:
        if action == "leave":
            if not existing:
                return web.json_response({"error": "not registered"}, status=400)
            db.remove_participant(existing["id"])
        elif action == "join":
            if existing:
                # Already on the game in some form. If they were 'maybe',
                # treat as confirm; if already confirmed/waitlist, no-op.
                if existing["status"] == "maybe":
                    db.confirm_from_maybe(existing["id"])
            else:
                db.add_participant(game["id"], added_by=user_id, member_id=user_id)
        elif action == "maybe":
            if existing:
                if existing["status"] == "maybe":
                    pass  # already maybe
                else:
                    # Switch from confirmed/waitlist → maybe: drop + re-add.
                    db.remove_participant(existing["id"])
                    db.add_maybe(game["id"], added_by=user_id, member_id=user_id)
            else:
                db.add_maybe(game["id"], added_by=user_id, member_id=user_id)
        elif action == "confirm":
            if not existing or existing["status"] != "maybe":
                return web.json_response(
                    {"error": "not currently maybe"}, status=400
                )
            db.confirm_from_maybe(existing["id"])
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    # Self-action by `user_id`, but a self-leave can auto-promote the
    # waitlist top — that promoted person is somebody else and needs a
    # DM. The diff helper handles all of that.
    await _notify_diffs(request, game["id"], before, user_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def post_self_guest(request: web.Request) -> web.Response:
    """Add a guest under the viewer's name.

    body: {"name": "Pat"}
    """
    game: dict = request["game"]
    user_id: int = request["user_id"]
    try:
        body = await request.json()
        name = str(body.get("name") or "").strip()
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"error": "bad json"}, status=400)

    if not name or len(name) > 40:
        return web.json_response({"error": "name must be 1-40 chars"}, status=400)

    before = db.get_participants(game["id"])
    try:
        db.add_participant(game["id"], added_by=user_id, guest_name=name)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    await _notify_diffs(request, game["id"], before, user_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def delete_self_guest(request: web.Request) -> web.Response:
    """Remove one of the viewer's own guests.

    Path: /api/game/<game_id>/guest/<pid>
    """
    game: dict = request["game"]
    user_id: int = request["user_id"]
    try:
        pid = int(request.match_info["pid"])
    except (KeyError, ValueError):
        return web.json_response({"error": "bad id"}, status=400)

    p = db.get_participant(pid)
    if not p or p["game_id"] != game["id"]:
        return web.json_response({"error": "not found"}, status=404)
    if p.get("member_id") is not None:
        return web.json_response({"error": "not a guest"}, status=400)
    if p.get("added_by") != user_id:
        return web.json_response({"error": "not your guest"}, status=403)

    before = db.get_participants(game["id"])
    db.remove_participant(pid)
    await _notify_diffs(request, game["id"], before, user_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


# ─────────────────────────────────────────────
# Admin routes
# ─────────────────────────────────────────────

async def admin_add_member(request: web.Request) -> web.Response:
    """Add any chat member to the game by their telegram_id.

    body: {"member_id": int}
    """
    err = await _require_admin(request)
    if err is not None:
        return err
    game: dict = request["game"]
    user_id: int = request["user_id"]
    chat_id = request["chat_id"]
    try:
        body = await request.json()
        target = int(body.get("member_id"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return web.json_response({"error": "bad json"}, status=400)

    if db.get_member(target) is None:
        return web.json_response({"error": "unknown member"}, status=404)
    # Verify target is a member of this chat
    if chat_id is not None and db.get_chat_member(chat_id, target) is None:
        return web.json_response({"error": "not in this chat"}, status=400)

    before = db.get_participants(game["id"])
    try:
        db.add_participant(game["id"], added_by=user_id, member_id=target)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    await _notify_diffs(request, game["id"], before, user_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def admin_add_guest(request: web.Request) -> web.Response:
    """Admin adds a guest with an arbitrary `added_by` (defaults to the admin).

    body: {"name": str, "added_by"?: int}  — added_by must be a member of this chat
    """
    err = await _require_admin(request)
    if err is not None:
        return err
    game: dict = request["game"]
    actor_id: int = request["user_id"]
    chat_id = request["chat_id"]
    try:
        body = await request.json()
        name = str(body.get("name") or "").strip()
        added_by = body.get("added_by")
        added_by = int(added_by) if added_by is not None else actor_id
    except (json.JSONDecodeError, TypeError, ValueError):
        return web.json_response({"error": "bad json"}, status=400)
    if not name or len(name) > 40:
        return web.json_response({"error": "name must be 1-40 chars"}, status=400)
    if chat_id is not None and db.get_chat_member(chat_id, added_by) is None:
        return web.json_response({"error": "adder not in chat"}, status=400)

    before = db.get_participants(game["id"])
    try:
        db.add_participant(game["id"], added_by=added_by, guest_name=name)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    await _notify_diffs(request, game["id"], before, actor_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def admin_remove_participant(request: web.Request) -> web.Response:
    """Admin removes any participant (member or guest) from the game."""
    err = await _require_admin(request)
    if err is not None:
        return err
    game: dict = request["game"]
    try:
        pid = int(request.match_info["pid"])
    except (KeyError, ValueError):
        return web.json_response({"error": "bad id"}, status=400)

    p = db.get_participant(pid)
    if not p or p["game_id"] != game["id"]:
        return web.json_response({"error": "not found"}, status=404)
    actor_id: int = request["user_id"]
    before = db.get_participants(game["id"])
    db.remove_participant(pid)
    await _notify_diffs(request, game["id"], before, actor_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def admin_move_participant(request: web.Request) -> web.Response:
    """Move a participant between confirmed / maybe / waitlist.

    body: {"status": "confirmed" | "maybe" | "waitlist"}

    Implementation notes:
      • confirmed → maybe / waitlist uses demote_to_waitlist or a manual
        remove+re-add (to avoid auto-promoting from waitlist when the
        admin is doing a deliberate reshuffle).
      • Moving INTO confirmed when full pushes someone to the top of the
        waitlist via swap_with_waitlist. We pick the lowest-position
        confirmed person to bump.
      • Moving to 'maybe' is implemented as remove + add_maybe so paid
        flags reset cleanly (maybes never pay).
    """
    err = await _require_admin(request)
    if err is not None:
        return err
    game: dict = request["game"]
    actor_id: int = request["user_id"]
    try:
        pid = int(request.match_info["pid"])
        body = await request.json()
        new_status = str(body.get("status") or "").lower()
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        return web.json_response({"error": "bad request"}, status=400)
    if new_status not in ("confirmed", "maybe", "waitlist"):
        return web.json_response({"error": "bad status"}, status=400)

    p = db.get_participant(pid)
    if not p or p["game_id"] != game["id"]:
        return web.json_response({"error": "not found"}, status=404)
    if p["status"] == new_status:
        # No-op, but still return current state
        payload = await _build_game_state(request)
        return web.json_response(payload)

    before = db.get_participants(game["id"])
    try:
        if new_status == "confirmed":
            if p["status"] == "maybe":
                db.confirm_from_maybe(pid)
            elif p["status"] == "waitlist":
                # Reuse the existing "promote / swap" logic.
                if db.confirmed_count(game["id"]) < game["max_players"]:
                    # Room available — just flip status + position.
                    with db.transaction() as conn:
                        new_pos = conn.execute(
                            "SELECT COALESCE(MAX(position), 0) + 1 AS n FROM participants WHERE game_id = ? AND status = 'confirmed'",
                            (game["id"],),
                        ).fetchone()["n"]
                        conn.execute(
                            "UPDATE participants SET status='confirmed', position=? WHERE id=?",
                            (new_pos, pid),
                        )
                    db._renumber(game["id"], "waitlist")  # noqa: SLF001
                else:
                    # Full — soft-swap with the bottom of confirmed (oldest).
                    participants = db.get_participants(game["id"])
                    confirmed = [x for x in participants if x["status"] == "confirmed"]
                    if not confirmed:
                        return web.json_response(
                            {"error": "no confirmed to swap"}, status=400
                        )
                    # Swap with the most-recently-confirmed (highest position)
                    target = max(confirmed, key=lambda x: x["position"])
                    db.swap_with_waitlist(target["id"], pid)
        elif new_status == "waitlist":
            if p["status"] == "confirmed":
                db.demote_to_waitlist(pid)
            elif p["status"] == "maybe":
                # Remove + add as waitlist (force_waitlist makes it bypass
                # the confirmed/waitlist routing).
                added_by = p.get("added_by") or actor_id
                member_id = p.get("member_id")
                guest_name = p.get("guest_name")
                db.remove_participant(pid)
                db.add_participant(
                    game["id"],
                    added_by=added_by,
                    member_id=member_id,
                    guest_name=guest_name,
                    force_waitlist=True,
                )
        elif new_status == "maybe":
            # remove + re-add as maybe (clears paid flag implicitly)
            added_by = p.get("added_by") or actor_id
            member_id = p.get("member_id")
            guest_name = p.get("guest_name")
            db.remove_participant(pid)
            db.add_maybe(
                game["id"],
                added_by=added_by,
                member_id=member_id,
                guest_name=guest_name,
            )
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    await _notify_diffs(request, game["id"], before, actor_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def admin_patch_game(request: web.Request) -> web.Response:
    """Edit game-level fields.

    body keys (any subset):
      • scheduled_for      ISO-8601 string parsed as the new datetime
      • duration_minutes   int 15..600  (how long the game runs)
      • location           non-empty string up to 100 chars
      • max_players        int 2..32
      • notes              string up to 200 chars; pass "" or null to clear
      • payment_amount_cents  int >= 0; 0 or null to clear payment
    """
    err = await _require_admin(request)
    if err is not None:
        return err
    game: dict = request["game"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"error": "bad json"}, status=400)

    from datetime import datetime, timedelta
    tz: ZoneInfo = request.app["tz"]
    bot: Bot = request.app["bot"]
    actor_id: int = request["user_id"]

    # Snapshot participants before any patch is applied. Only
    # max_players actually shuffles the roster, but we always diff so
    # behavior stays predictable when more fields gain side-effects.
    before = db.get_participants(game["id"])

    # Apply each field independently so partial updates work.
    if "scheduled_for" in body:
        raw = body["scheduled_for"]
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
        except (TypeError, ValueError):
            return web.json_response({"error": "bad scheduled_for"}, status=400)
        if dt < datetime.now(tz) - timedelta(minutes=10):
            return web.json_response({"error": "time is in the past"}, status=400)
        db.update_game_time(game["id"], dt)

    if "duration_minutes" in body:
        try:
            mins = int(body["duration_minutes"])
        except (TypeError, ValueError):
            return web.json_response({"error": "bad duration_minutes"}, status=400)
        # 15 min lower bound rules out the "0" / "1 minute" misclick;
        # 600 min (10h) upper bound rules out runaway values without
        # constraining real-world usage.
        if not (15 <= mins <= 600):
            return web.json_response({"error": "duration_minutes 15-600"}, status=400)
        db.update_game_duration(game["id"], mins)

    if "location" in body:
        loc = str(body["location"] or "").strip()
        if not loc or len(loc) > 100:
            return web.json_response({"error": "location 1-100 chars"}, status=400)
        db.update_game_location(game["id"], loc)

    if "max_players" in body:
        try:
            n = int(body["max_players"])
        except (TypeError, ValueError):
            return web.json_response({"error": "bad max_players"}, status=400)
        if not (2 <= n <= 32):
            return web.json_response({"error": "max_players 2-32"}, status=400)
        db.update_game_max(game["id"], n)

    if "notes" in body:
        raw = body["notes"]
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            db.update_game_notes(game["id"], None)
        else:
            text = str(raw).strip()
            if len(text) > 200:
                return web.json_response({"error": "notes <=200 chars"}, status=400)
            db.update_game_notes(game["id"], text)

    if "payment_amount_cents" in body:
        raw = body["payment_amount_cents"]
        if raw is None:
            db.update_game_payment_amount(game["id"], None)
        else:
            try:
                cents = int(raw)
            except (TypeError, ValueError):
                return web.json_response({"error": "bad payment"}, status=400)
            if cents < 0:
                return web.json_response({"error": "negative payment"}, status=400)
            db.update_game_payment_amount(
                game["id"], cents if cents > 0 else None
            )

    # Re-fetch the game so subsequent state-building uses the new values.
    request["game"] = db.get_game(game["id"])
    await _notify_diffs(request, game["id"], before, actor_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def admin_delete_game(request: web.Request) -> web.Response:
    """Delete the game and notify previously-confirmed members."""
    err = await _require_admin(request)
    if err is not None:
        return err
    game: dict = request["game"]
    actor_id: int = request["user_id"]
    bot: Bot = request.app["bot"]
    tz: ZoneInfo = request.app["tz"]

    participants = db.get_participants(game["id"])
    affected = [p for p in participants if p.get("member_id")]
    db.delete_game(game["id"])

    # Best-effort DMs — the actor saw the game in the mini-app; they
    # don't need a chat-level confirmation. Anyone signed up gets DM'd.
    actor = db.get_member(actor_id)
    actor_name = actor["display_name"] if actor else "An admin"
    when = views.format_when(
        game["scheduled_for"], tz, game.get("duration_minutes"),
    )
    text = (
        f"🗑 The game on <b>{when}</b> "
        f"@ {game['location']} was deleted by {actor_name}."
    )
    for p in affected:
        try:
            await bot.send_message(
                chat_id=p["member_id"], text=text, parse_mode="HTML"
            )
        except Exception as e:
            log.info("Couldn't DM user %s about delete: %s", p["member_id"], e)

    # Best-effort: replace the original card with a tombstone so the
    # group sees the deletion. We don't have a CallbackQuery here — go
    # straight through edit_message_text.
    if game.get("chat_id") and game.get("message_id"):
        try:
            await bot.edit_message_text(
                chat_id=game["chat_id"],
                message_id=game["message_id"],
                text=(
                    f"🗑 Game deleted: <s>{when} "
                    f"@ {game['location']}</s>"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            log.info("Couldn't edit deleted-game card: %s", e)

    return web.json_response({"deleted": True})


async def admin_pay_toggle(request: web.Request) -> web.Response:
    """Toggle the paid flag on a confirmed participant."""
    err = await _require_admin(request)
    if err is not None:
        return err
    game: dict = request["game"]
    try:
        pid = int(request.match_info["pid"])
    except (KeyError, ValueError):
        return web.json_response({"error": "bad id"}, status=400)
    p = db.get_participant(pid)
    if not p or p["game_id"] != game["id"]:
        return web.json_response({"error": "not found"}, status=404)
    db.toggle_participant_paid(pid)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


# ─────────────────────────────────────────────
# App factory + runner
# ─────────────────────────────────────────────

def create_app(
    *,
    bot: Bot,
    bot_token: str,
    tz: ZoneInfo,
    refresh_card: Callable[[int], Awaitable[None]],
) -> web.Application:
    """Build the aiohttp app.

    `refresh_card(game_id)` is invoked after every mutating API call so the
    in-chat card stays in sync with the mini-app's view. It runs on the
    same asyncio loop as the bot, so it can call bot.edit_message_text
    directly.
    """
    app = web.Application(middlewares=[auth_middleware])
    app["bot"] = bot
    app["bot_token"] = bot_token
    app["tz"] = tz
    app["refresh_card"] = refresh_card

    # Static / health
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    app.router.add_get("/register", _serve_static("register.html"))
    app.router.add_get("/manage", _serve_static("manage.html"))

    # Game state (any chat member)
    app.router.add_get("/api/game/{game_id}", get_game_state)

    # Member-facing mutations
    app.router.add_post("/api/game/{game_id}/register", post_self_register)
    app.router.add_post("/api/game/{game_id}/guest", post_self_guest)
    app.router.add_delete("/api/game/{game_id}/guest/{pid}", delete_self_guest)

    # Admin-facing mutations
    app.router.add_post("/api/game/{game_id}/admin/add_member", admin_add_member)
    app.router.add_post("/api/game/{game_id}/admin/add_guest", admin_add_guest)
    app.router.add_delete(
        "/api/game/{game_id}/admin/participant/{pid}", admin_remove_participant
    )
    app.router.add_post(
        "/api/game/{game_id}/admin/move/{pid}", admin_move_participant
    )
    app.router.add_patch("/api/game/{game_id}", admin_patch_game)
    app.router.add_delete("/api/game/{game_id}", admin_delete_game)
    app.router.add_post(
        "/api/game/{game_id}/admin/pay_toggle/{pid}", admin_pay_toggle
    )

    return app


async def run_http_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    """Start the HTTP server. Returns the runner so we can shut it down cleanly."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info(f"HTTP server listening on {host}:{port}")
    return runner
