# CTFtime Discord Bot

An automated Discord bot for CTF teams: every week it posts the list of upcoming
CTFtime events for the whole team to **vote** on, then automatically picks the
winning event and creates a **Discord Scheduled Event**.

## Features

| Time (UTC) | What the bot does |
| --- | --- |
| **Every Tuesday, 00:01** | Post up to **3** online CTF events for the next 7 days (most participants first), one message per event with ✅ / ❌ reactions to vote. |
| **Every Friday, 00:01** | Close voting → pick **1 winning event** and create a Discord Scheduled Event. |
| **1st of every month, 00:01** | Post the team's own CTFtime ranking for the current year. |

**How the winning event is chosen** (in priority order):
1. Most ✅ votes.
2. On a tie (or if nobody voted) → the event with more **participants** (registered teams).
3. If participants are also equal → the event with the higher **weight** (weight = 0 is fine).

**Slash commands:**
- `/leaderboard` — show the team's own CTFtime ranking (any time).
- `/leaderboardtop10` — show the CTFtime world Top 10 (on demand, not periodic).
- `/testweekly` — *(Admin)* run the post-list + open-vote job immediately.
- `/testclose` — *(Admin)* run the close-vote job immediately.

## Requirements

- Python **3.10+**
- The packages in [requirement.txt](requirement.txt): `discord.py>=2.3`, `aiohttp`, `APScheduler`, `python-dotenv`
- A **Discord Application + Bot** (see the guide below)
- A **Team ID** on CTFtime

## Installation

```bash
# 1. Install dependencies
pip install -r requirement.txt

# 2. Create the .env file from the template, then fill in the real values
cp .env.example .env      # Windows PowerShell: Copy-Item .env.example .env

# 3. Run the bot
python main.py
```

The bot saves the state of open votes to `state.json` (auto-generated, already
ignored by `.gitignore`). **Never commit `.env`.**

---

## `.env` configuration — how to get each value

The `.env` file has 4 required variables. If any of them is missing, the bot
fails at startup.

```dotenv
# Bot token (Discord Developer Portal -> Bot -> Reset Token)
DISCORD_TOKEN=

# Discord server ID (enable Developer Mode -> right-click server name -> Copy Server ID)
GUILD_ID=

# Channel ID used for event announcements + voting (right-click channel -> Copy Channel ID)
ANNOUNCE_CHANNEL_ID=

# Your CTFtime team ID (the number in the URL https://ctftime.org/team/XXXXX)
CTFTIME_TEAM_ID=
```



### `DISCORD_TOKEN` — the bot token

- Go to the Discord Developer Portal → **New Application** → **Bot** tab.
- Click **Reset Token** → **Copy** it (shown only once) → paste into `.env`. **Never share or commit it.**
- Invite the bot via **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`, and permissions **View Channels, Send Messages, Embed Links, Add Reactions, Read Message History, Mention Everyone** *(to ping @everyone when voting opens)*, **Manage Events** *(required for Scheduled Events)*.
- Copy URL -> Open it -> Choose server for bot active.

---
## Security notes

- `.env` and `state.json` are already in [.gitignore](.gitignore) — **do not commit** them.
- If you accidentally leak the token, **Reset Token** immediately in the Developer Portal.
