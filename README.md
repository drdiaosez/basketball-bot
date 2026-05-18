# Basketball Bot

Telegram bot for organizing basketball pickup games — signups (Confirmed / Maybe / Waitlist), guests, scheduling, and payment tracking.

Forked from [pickleball-bot](https://github.com/drdiaosez/pickleball-bot) with the moneyball mini-app stripped out and a "Maybe" tier added. The game card itself now opens **two Telegram Mini Apps** — `/register` for self-service signups and `/manage` for chat-admin game edits — instead of the long callback-button strip on previous versions.

## What it does

- **Game scheduling**: `/newgame` (date, time, location, max players, optional per-person payment, notes). Multiple concurrent games supported.
- **Three-tier signups**: tap-to-join cards with **Confirmed** / **Maybe** / **Waitlist** sections.
  - Maybes don't count toward the player cap and don't owe payment. They tap **Confirm** when they're sure (or **Leave** to drop).
  - Waitlist auto-promotes to Confirmed when a slot opens.
- **Guests**: members can add non-member guests (e.g. "John's friend Mike") from inside the Register mini-app, and remove guests they added.
- **Editing**: change time / location / max / notes / payment, or delete a game, via the Manage mini-app (chat admins only).
- **Browsing**: `/games`, `/mygames`, `/past`, `/week [next | last | 5/18]`.
- **Payment tracking**: optional per-person amount; the in-chat "💰 Paid" picker is unchanged. Maybes never pay until they confirm.
- **Multi-chat**: the bot can be in any number of groups; data is scoped per chat. DMs that span multiple groups get a "which group?" picker.

## Card layout

Each `/games` card carries up to four buttons:

| Button | Who | What it does |
|---|---|---|
| 📝 **Register** / ✏️ **Update Registration** | Any chat member | Opens `/register` mini-app: join/leave, mark maybe, confirm a maybe, add/remove your own guests. Label flips to *Update* once you've registered yourself or added a guest. |
| ⚙ **Manage (admin only)** | Chat admins | Opens `/manage` mini-app: move/remove anyone, add any chat member, edit time/location/max/notes/payment, mark people paid, delete the game. Non-admins see an "admin only" splash. |
| 💰 **Paid** | Anyone (only if payment set) | Unchanged in-chat picker — flip ✅/⬜ per confirmed player. |
| 🔄 **Refresh** | Anyone | Re-renders the card with the latest state. |

## Architecture

A single Python process runs the Telegram polling loop **and** a small aiohttp server on the same asyncio loop. SQLite handles persistence. The aiohttp server (default `127.0.0.1:8081`) sits behind a reverse proxy (Caddy, nginx, …) that terminates TLS and forwards `https://<PUBLIC_URL>/` to it. Telegram requires HTTPS for mini-app buttons in inline keyboards.

```
  Telegram → bot polling loop ─┬─→ db.sqlite
                               └─→ aiohttp on :8081 ←─ Caddy/Nginx ←─ Telegram WebView
                                   /register, /manage, /api/*
```

The mini-apps authenticate every API call by verifying Telegram's HMAC-signed `initData` blob against `BOT_TOKEN`. There is no separate session/cookie store — every request brings its own proof of identity.

## Deployment (alongside an existing pickleball-bot on the same droplet)

This bot is designed to coexist on the same droplet as the pickleball-bot. They share the host but are completely separate processes, with their own databases, BotFather tokens, systemd services, and HTTP ports. Pickleball-bot's existing Caddy install can grow one extra block for this bot.

### 1. Create the bot in BotFather

DM @BotFather:
- `/newbot` → pick a name like "Saturday Hoops Bot" and a username like `SaturdayHoopsBot`
- Save the token it gives you
- `/setcommands` → paste the contents of `bot_commands.txt`
- `/setprivacy` → **Disable** (so the bot sees all messages in the group)

Then add the bot to your basketball group chat (and give it admin if you want the `chat_member` updates that catch when people leave). For the **Manage** mini-app to recognize chat admins, the bot does not need to be an admin itself, but it does need permission to call `getChatAdministrators` (granted by default to any group member).

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

# Mini-app HTTP server — required for Register/Manage buttons to work.
# PUBLIC_URL is the HTTPS origin Telegram clients will reach (the reverse
# proxy's external address). HTTP_HOST/PORT are where this process binds
# locally; the proxy forwards to them.
PUBLIC_URL=https://bball.example.com
HTTP_HOST=127.0.0.1
HTTP_PORT=8081
```

That's it — no group ID needed. When you add the bot to your basketball group, it auto-registers the chat into its internal `chats` table and starts answering commands there. You can add it to additional groups later; each one self-registers the same way.

### 3a. Reverse-proxy config (Caddy)

Pick a subdomain that's distinct from any other bot on the box (the pickleball-bot uses its own). Add this block to the existing `Caddyfile`:

```caddy
bball.example.com {
    reverse_proxy 127.0.0.1:8081
    encode zstd gzip
    log {
        output file /var/log/caddy/bball.log
        format console
    }
}
```

`sudo systemctl reload caddy` to apply. Caddy will auto-provision a Let's Encrypt cert the first time the DNS resolves to this host. **HTTPS is mandatory** — Telegram refuses to launch web_app buttons that point at plain `http://`.

### 3b. Register the two Mini Apps in BotFather

Inline keyboard buttons in **groups** can't use `web_app` directly — Telegram only allows that in private chats. The group card therefore opens each mini-app via a `t.me/<bot_username>/<short_name>?startapp=<game_id>` deep link, which requires registering each app inside BotFather. Do this once per bot:

In @BotFather:
- `/newapp` → pick the basketball bot → **Title:** "Register" → **Short description:** "Join, leave, or maybe a game" → upload a 640×360 PNG (or skip) → **Web App URL:** `https://bball.example.com/register` → **Short name:** `register`.
- `/newapp` → pick the basketball bot → **Title:** "Manage" → **Short description:** "Admin tools for a game" → upload an icon (or skip) → **Web App URL:** `https://bball.example.com/manage` → **Short name:** `manage`.

The short names **must be exactly `register` and `manage`** (lowercase, no separators) — they're hard-coded in `bot/views.py` as `REGISTER_APP_SHORT_NAME` / `MANAGE_APP_SHORT_NAME`. If you want different names, change them in both places.

You can verify the apps by visiting `https://t.me/<bot_username>/register?startapp=1` and `https://t.me/<bot_username>/manage?startapp=1` in a browser — both should resolve to a Telegram deep link.

> **You do not also need `/setdomain`.** That command is for `web_app` inline buttons in *private* chats, which this bot doesn't currently use.

### 3c. (Optional) Add a Menu Button shortcut

`/setmenubutton` → pick the bot → "URL" → label it "Open" and use `https://t.me/<bot_username>/register`. Users who DM the bot get a one-tap shortcut to the mini-app. They'll still need to pick a specific game once inside — leaving the deep link without a `startapp` is fine; the mini-app shows a friendly "open from a game card" message when no game_id is present.

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

Look for `Bot starting…`, `Telegram polling started`, and `HTTP server listening on 127.0.0.1:8081`. The pickleball-bot service keeps running independently — they don't share state, the SQLite databases are separate, and they bind different ports (`pickleball-bot` already owns `:8080`, this one defaults to `:8081`).

You can sanity-check that the HTTP layer is reachable with:

```bash
curl https://bball.example.com/healthz   # should print "ok"
curl -I https://bball.example.com/register   # 200, HTML
```

### 5. Try it

In the basketball group: `/newgame`. The card now shows **📝 Register**, **⚙ Manage (admin only)**, **🔄 Refresh** (plus **💰 Paid** if you set a payment amount). Tap Register to walk through joining/leaving, going maybe, and adding/removing guests inside the mini-app. Tap Manage as a chat admin to move people between Confirmed/Maybe/Waitlist, edit game details, or delete the game.

> **Old cards in chat history.** Cards posted before this upgrade still have their original chat buttons (Join / Maybe / Add Member / etc.). All of those callback handlers are still wired up, so old cards keep working until they scroll off — the new mini-app flow only kicks in for newly-posted cards. If you want to retire an old card sooner, just `/newgame` and the old one will rot harmlessly.

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
