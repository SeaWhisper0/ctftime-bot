"""
CTFtime Discord Bot
===================
Workflow:
  - Every Monday (00:01 UTC): post up to 3 CTFtime events starting within the next
    week (most participants first) to the announcement channel, one message per
    event with ✅/❌ reactions to vote.
  - Every Friday (00:01 UTC): close voting — pick EXACTLY 1 winning event:
      1) the event with the most ✅ votes;
      2) on a tie (or if nobody voted): the event with more participants;
      3) if participants are equal: the event with the highest weight (weight=0 is fine).
    The winning event gets a Discord Scheduled Event created automatically.
  - If there is no event this week: just announce "no events" and do nothing else.
  - 1st of every month (00:01 UTC): automatically post the team's own ranking.
  - /leaderboard command: check the team's own CTFtime ranking at any time.
  - /leaderboardtop10 command: show the CTFtime world top 10 on demand (not periodic).

Requirements: Python 3.10+, discord.py >= 2.3, aiohttp, apscheduler
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands
from dotenv import load_dotenv

# Load variables from the .env file (placed next to main.py)
load_dotenv()

# ============================ CONFIG ============================
# All sensitive values come from the .env file — DO NOT hardcode them here.
TOKEN = os.environ["DISCORD_TOKEN"]  # required; missing value fails at startup
GUILD_ID = int(os.environ["GUILD_ID"])
ANNOUNCE_CHANNEL_ID = int(os.environ["ANNOUNCE_CHANNEL_ID"])
CTFTIME_TEAM_ID = int(os.environ["CTFTIME_TEAM_ID"])

STATE_FILE = "state.json"   # persist open vote messages across restarts
CTFTIME_API = "https://ctftime.org/api/v1"
# CTFtime blocks the default aiohttp/python User-Agent -> set a custom UA.
HTTP_HEADERS = {"User-Agent": "ctftime-discord-bot/1.0 (self-hosted team bot)"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ctftime-bot")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
scheduler = AsyncIOScheduler(timezone="UTC")


# ============================ UTILITIES ============================
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"open_votes": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


async def api_get(session: aiohttp.ClientSession, path: str, params: dict | None = None):
    async with session.get(f"{CTFTIME_API}{path}", params=params, headers=HTTP_HEADERS) as r:
        r.raise_for_status()
        return await r.json()


async def fetch_upcoming_events(days: int = 7) -> list[dict]:
    """Fetch events starting within the next `days` days (online events only)."""
    now = datetime.now(timezone.utc)
    params = {
        "limit": 100,
        "start": int(now.timestamp()),
        "finish": int((now + timedelta(days=days)).timestamp()),
    }
    async with aiohttp.ClientSession() as session:
        events = await api_get(session, "/events/", params)
    # Drop onsite-only events for brevity; remove this filter to show onsite too.
    return [e for e in events if e.get("onsite") is False]


def event_embed(e: dict) -> discord.Embed:
    start = datetime.fromisoformat(e["start"])
    finish = datetime.fromisoformat(e["finish"])
    emb = discord.Embed(
        title=e["title"],
        url=e["ctftime_url"],
        description=e.get("description", "")[:300],
        color=0xE74C3C,
    )
    emb.add_field(name="Start", value=f"<t:{int(start.timestamp())}:F>", inline=True)
    emb.add_field(name="Finish", value=f"<t:{int(finish.timestamp())}:F>", inline=True)
    emb.add_field(name="Format", value=e.get("format", "?"), inline=True)
    emb.add_field(name="Weight", value=str(e.get("weight", "?")), inline=True)
    if e.get("url"):
        emb.add_field(name="Website", value=e["url"], inline=False)
    emb.set_footer(text="Vote ✅ to join, ❌ to skip — voting closes 00:01 UTC Friday")
    return emb


async def build_team_embed() -> discord.Embed:
    """Embed with your team's CTFtime ranking for the current year."""
    year = str(datetime.now(timezone.utc).year)
    async with aiohttp.ClientSession() as session:
        team = await api_get(session, f"/teams/{CTFTIME_TEAM_ID}/")

    emb = discord.Embed(
        title=f"🏆 CTFtime Ranking {year}",
        url=f"https://ctftime.org/team/{CTFTIME_TEAM_ID}",
        color=0xF1C40F,
    )
    rating = team.get("rating", {}).get(year, {})
    place = rating.get("rating_place", "N/A")
    points = rating.get("rating_points", 0)
    emb.add_field(
        name=f"Team: {team.get('name', '?')}",
        value=f"World rank: **#{place}**\nRating points: **{points:.3f}**"
        if isinstance(points, (int, float))
        else f"World rank: **#{place}**",
        inline=False,
    )
    emb.timestamp = datetime.now(timezone.utc)
    return emb


async def build_top10_embed() -> discord.Embed:
    """Embed with the CTFtime world top 10 for the current year."""
    year = str(datetime.now(timezone.utc).year)
    async with aiohttp.ClientSession() as session:
        top = await api_get(session, f"/top/{year}/")

    emb = discord.Embed(
        title=f"🌍 CTFtime World Top 10 — {year}",
        url="https://ctftime.org/stats/",
        color=0xF1C40F,
    )
    lines = []
    for i, t in enumerate(top.get(year, [])[:10], start=1):
        lines.append(f"`{i:>2}.` **{t['team_name']}** — {t['points']:.2f} pts")
    emb.description = "\n".join(lines) if lines else "No data available."
    emb.timestamp = datetime.now(timezone.utc)
    return emb


# ======================= JOB 1: MONDAY — POST LIST + OPEN VOTE =======================
async def weekly_post_and_vote():
    channel = client.get_channel(ANNOUNCE_CHANNEL_ID)
    if channel is None:
        log.error("Announcement channel %s not found", ANNOUNCE_CHANNEL_ID)
        return

    events = await fetch_upcoming_events(days=7)
    if not events:
        await channel.send("No online CTF events on CTFtime this week.")
        return

    # Only put up the 3 best events for voting: most participants, then weight (weight=0 is fine).
    events.sort(
        key=lambda e: (int(e.get("participants", 0) or 0), float(e.get("weight", 0) or 0)),
        reverse=True,
    )
    events = events[:3]

    await channel.send(
        f"📅 **Top {len(events)} CTF events in the next 7 days**\n"
        f"Vote ✅ on the event you want to play — **the most-voted event will be picked** "
        f"at **00:01 UTC Friday** (ties are broken by number of participants, then weight).\n\n"
        f"@everyone vote for event this weekend"
    )

    state = load_state()
    state["open_votes"] = []
    for e in events:
        msg = await channel.send(embed=event_embed(e))
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")
        state["open_votes"].append(
            {
                "message_id": msg.id,
                "event_id": e["id"],
                "title": e["title"],
                "start": e["start"],
                "finish": e["finish"],
                "url": e.get("url") or e["ctftime_url"],
                "ctftime_url": e["ctftime_url"],
                "format": e.get("format", ""),
                "weight": e.get("weight", 0) or 0,
                "participants": e.get("participants", 0) or 0,
            }
        )
    save_state(state)
    log.info("Posted %d events and opened voting.", len(events))


# ======================= JOB 2: FRIDAY 00:01 UTC — CLOSE VOTE + SCHEDULE =======================
async def close_votes_and_schedule():
    channel = client.get_channel(ANNOUNCE_CHANNEL_ID)
    guild = client.get_guild(GUILD_ID)
    if channel is None or guild is None:
        log.error("Missing channel or guild.")
        return

    state = load_state()
    open_votes = state.get("open_votes", [])
    if not open_votes:
        # No events this week -> nothing to do.
        log.info("No open votes, skipping the close-vote job.")
        return

    # Count ✅ votes for each event and drop events that already started (can't schedule).
    now = datetime.now(timezone.utc)
    candidates = []
    for item in open_votes:
        try:
            msg = await channel.fetch_message(item["message_id"])
        except discord.NotFound:
            continue

        yes = 0
        for reaction in msg.reactions:
            if str(reaction.emoji) == "✅":
                yes = reaction.count - 1  # subtract the bot's own reaction
        item["yes"] = yes
        if datetime.fromisoformat(item["start"]) > now:
            candidates.append(item)

    if not candidates:
        await channel.send("🔔 No schedulable events left (all have already started).")
        state["open_votes"] = []
        save_state(state)
        return

    # Pick EXACTLY 1 winning event by priority:
    #   1) most ✅ votes  2) most participants  3) highest weight (weight=0 is fine)
    # (when nobody votes, yes = 0 for all -> automatically falls through to the participants tie-break)
    candidates.sort(
        key=lambda x: (x["yes"], int(x["participants"]), float(x["weight"])),
        reverse=True,
    )
    winner = candidates[0]

    # Determine the reason it won, for transparency.
    top_votes = [c for c in candidates if c["yes"] == winner["yes"]]
    if winner["yes"] > 0 and len(top_votes) == 1:
        reason = f"most votes ({winner['yes']} ✅)"
    else:
        top_part = [c for c in top_votes if int(c["participants"]) == int(winner["participants"])]
        if len(top_part) == 1:
            reason = (
                f"tied at {winner['yes']} votes → most participants ({winner['participants']} teams)"
                if winner["yes"] > 0
                else f"no votes → most participants ({winner['participants']} teams)"
            )
        else:
            reason = (
                f"tied on votes & participants → highest weight ({winner['weight']})"
            )

    # Create a Discord Scheduled Event for the winner (if it doesn't exist yet).
    start = datetime.fromisoformat(winner["start"])
    end = datetime.fromisoformat(winner["finish"])
    existing_events = {ev.name for ev in guild.scheduled_events}
    if winner["title"] not in existing_events:
        await guild.create_scheduled_event(
            name=winner["title"],
            description=(
                f"{winner['ctftime_url']}\n"
                f"Format: {winner['format']} | Weight: {winner['weight']} | "
                f"Registered teams: {winner['participants']}\n"
                f"Votes: ✅ {winner['yes']}"
            ),
            start_time=start,
            end_time=end,
            entity_type=discord.EntityType.external,
            location=winner["url"],
            privacy_level=discord.PrivacyLevel.guild_only,
        )

    summary = (
        f"🔔 **Voting closed for this week!**\n"
        f"🏆 Selected event: **{winner['title']}**\n"
        f"Reason: {reason}\n"
        f"🕐 Start: <t:{int(start.timestamp())}:F>\n"
        f"🔗 {winner['ctftime_url']}\n\n"
        f"👉 Scheduled Event created — open the server's **Events** tab and click *Interested* to get reminders!"
    )
    await channel.send(summary)

    state["open_votes"] = []
    save_state(state)
    log.info("Voting closed: '%s' won (%s).", winner["title"], reason)


# ======================= JOB 3: 1ST OF MONTH 00:01 UTC — POST LEADERBOARD =======================
async def monthly_leaderboard():
    channel = client.get_channel(ANNOUNCE_CHANNEL_ID)
    if channel is None:
        return
    try:
        emb = await build_team_embed()
        await channel.send("📊 **Monthly CTFtime ranking report**", embed=emb)
    except Exception:
        log.exception("Failed to send the monthly leaderboard")


# ======================= /leaderboard + /leaderboardtop10 COMMANDS =======================
@tree.command(name="leaderboard", description="Show the team's own CTFtime ranking")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        emb = await build_team_embed()
        await interaction.followup.send(embed=emb)
    except Exception as exc:
        await interaction.followup.send(f"⚠️ Could not fetch data from CTFtime: `{exc}`")


@tree.command(name="leaderboardtop10", description="Show the CTFtime world top 10 teams")
async def leaderboardtop10(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        emb = await build_top10_embed()
        await interaction.followup.send(embed=emb)
    except Exception as exc:
        await interaction.followup.send(f"⚠️ Could not fetch data from CTFtime: `{exc}`")


# Helper commands to test immediately without waiting until Monday / Friday.
@tree.command(name="testweekly", description="(Admin) Run the post-list + vote job immediately")
@app_commands.checks.has_permissions(manage_guild=True)
async def testweekly(interaction: discord.Interaction):
    await interaction.response.send_message("Running the Monday job...", ephemeral=True)
    await weekly_post_and_vote()


@tree.command(name="testclose", description="(Admin) Run the close-vote job immediately")
@app_commands.checks.has_permissions(manage_guild=True)
async def testclose(interaction: discord.Interaction):
    await interaction.response.send_message("Closing votes...", ephemeral=True)
    await close_votes_and_schedule()


# ============================ STARTUP ============================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    await tree.sync()  # global sync (may take up to 1h to appear; the guild copy is instant)

    if not scheduler.running:
        # Every Monday 00:01 UTC — post list + open vote
        scheduler.add_job(weekly_post_and_vote, CronTrigger(day_of_week="mon", hour=0, minute=1))
        # Every Friday 00:01 UTC — close vote + create scheduled event
        scheduler.add_job(close_votes_and_schedule, CronTrigger(day_of_week="fri", hour=0, minute=1))
        # 1st of every month 00:01 UTC — post leaderboard
        scheduler.add_job(monthly_leaderboard, CronTrigger(day=1, hour=0, minute=1))
        scheduler.start()

    log.info("Bot ready: %s", client.user)


if __name__ == "__main__":
    client.run(TOKEN)
