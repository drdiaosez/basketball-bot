# Basketball Bot

Telegram bot for organizing basketball pickup games — signups (Confirmed / Maybe / Waitlist), guests, scheduling, and payment tracking.

Forked from [pickleball-bot](https://github.com/drdiaosez/pickleball-bot) with the moneyball mini-app stripped out and a "Maybe" tier added.

## What it does

- **Game scheduling**: `/newgame` (date, time, location, max players, optional per-person payment, notes). Multiple concurrent games supported.
- **Three-tier signups**: tap-to-join cards with **Confirmed** / **Maybe** / **Waitlist** sections.
  - Maybes don't count toward the player cap and don't owe payment. They tap **Confirm** when they're sure (or **Leave** to drop).
  - Waitlist auto-promotes to Confirmed when a slot opens.
- **Guests**: members can add non-member guests (e.g. "John's friend Mike").
- **Editing**: change time / location / max / notes / payment, or delete a game, via the Manage view.
- **Browsing**: `/games`, `/mygames`, `/past`, `/week [next | last | 5/18]`.
- **Payment tracking**: optional per-person amount; per-confirmed-player ✅/⬜ paid toggle. Maybes never pay until they confirm.
- **Multi-chat**: the bot can be in any number of groups; data is scoped per chat. DMs that span multiple groups get a "which group?" picker.

## Architecture

The Telegram polling loop runs as a single Python process. SQLite handles persistence. No HTTP server, no Caddy, no domain needed — basketball games don't need a mini-app, so the bot just talks to Telegram and back.

```
  Telegram → bot polling loop → db.sqlite
```

## Deployment (alongside an existing pickleball-bot on the same droplet)

This bot is designed to coexist on the same droplet as the pickleball-bot. They share the host but are completely separate processes, with their own databases, BotFather tokens, and systemd services. The pickleball-bot's existing Caddy/HTTP setup is untouched.

### 1. Create the bot in BotFather

DM @BotFather:
- `/newbot` → pick a name like "Saturday Hoops Bot" and a username like `SaturdayHoopsBot`
- Save the token it gives you
- `/setcommands` → paste the contents of `bot_commands.txt`
- `/setprivacy` → **Disable** (so the bot sees all messages in the group)

Then add the bot to your basketball group chat (and give it admin if you want the `chat_member` updates that catch when people leave).

### 2. Clone and install on the droplet

SSH in as the `bot` user (the same one that runs pickleball-bot), then:

```bash
cd /home/bot
git clone <your-basketball-bot-repo>.git basketball-bot
cd basketball-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Create `.env`

```bash
nano /home/bot/basketball-bot/.env
```

```
BOT_TOKEN=<from-BotFather>
DB_PATH=/home/bot/basketball-bot/db.sqlite
TIMEZONE=America/Los_Angeles
```

That's it — no group ID needed. When you add the bot to your basketball group, it auto-registers the chat into its internal `chats` table and starts answering commands there. You can add it to additional groups later; each one self-registers the same way.

<details>
<summary>What about <code>ALLOWED_GROUP_ID</code>?</summary>

It's a legacy single-group lockdown var inherited from the parent pickleball-bot. The current auth model uses the `chats` SQLite table (populated when the bot is added to a group); `ALLOWED_GROUP_ID` is only consulted as a fallback when the table is empty. For a fresh basketball-bot install you can ignore it entirely.

Set it (e.g., `ALLOWED_GROUP_ID=-1002345678901`) only if you want extra paranoia — locking the bot to one specific group ID even before it's added there, so a misconfigured token can't accidentally answer in a wrong group. To find the chat ID: add @userinfobot to your group, or run the bot in the foreground (`python -m bot.main`) and watch the logs.

</details>

### 4. Add a systemd service

```bash
sudo nano /etc/systemd/system/basketball-bot.service
```

```ini
[Unit]
Description=Basketball Bot
After=network.target

[Service]
Type=simple
User=bot
WorkingDirectory=/home/bot/basketball-bot
EnvironmentFile=/home/bot/basketball-bot/.env
ExecStart=/home/bot/basketball-bot/venv/bin/python -m bot.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable basketball-bot
sudo systemctl start basketball-bot
sudo journalctl -u basketball-bot -n 30 --no-pager -f
```

Look for `Bot starting…` and `Telegram polling started`. The pickleball-bot service keeps running independently — they don't share state and don't compete for ports (this one doesn't bind any).

### 5. Try it

In the basketball group: `/newgame`, then tap **🤔 Maybe** on the card to see the new tier in action.

## Commands

| Command | Description |
|---|---|
| `/newgame` | Schedule a new game |
| `/games` | List upcoming games |
| `/mygames` | Games you're signed up for |
| `/past` | Recent past games |
| `/week [next \| last \| date]` | Games in a specific Mon-Sun week |
| `/spend` | Per-member spend total for the current calendar year (ranked) |
| `/balance` | Outstanding-balance report — everyone (members + guests) with unpaid confirmed signups, ranked by amount owed, with per-game breakdown |
| `/help` | Show command list |

Everything else — joining, going maybe, confirming, leaving, adding guests, swapping people in, marking paid — happens through the buttons on each game card.

## Signup state machine

| From | Tap | To |
|---|---|---|
| (not on game) | Join | Confirmed (if room) or Waitlist (if full) |
| (not on game) | Maybe | Maybe |
| Maybe | Confirm | Confirmed (if room) or Waitlist (if full) |
| Maybe | Leave | (off the game) |
| Confirmed | Leave | (off the game; top of Waitlist auto-promotes) |
| Waitlist | Leave | (off the game) |

Maybes never count toward `max_players` and never owe payment. Only Confirmed players show ✅/⬜ paid badges.

## What's different from pickleball-bot

- ❌ No moneyball mini-app, no leaderboard, no `/merge` admin command
- ❌ No HTTP server, no Caddy/domain requirement
- ✅ Three signup tiers (Confirmed / Maybe / Waitlist) instead of two
- ✅ Default `max_players` is 15 (a comfortable run for an open-gym) instead of 4
- ✅ Default location pre-populated as "Quartz Sport in Carson"
- ✅ Default game time pre-populated as next Monday at 8:30 PM (one-tap to accept)

The DB schema is otherwise compatible; if you ever wanted to feed this bot from a pickleball-bot db dump, the `participants` and `games` tables would import cleanly.
