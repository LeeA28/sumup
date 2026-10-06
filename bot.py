import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
import openai
from discord import app_commands
from dotenv import load_dotenv

import state
from summarizer import summarize_transcript
from timeutils import (
    ALL_ZONES,
    SinceError,
    describe_since,
    format_clock,
    parse_since,
    search_zones,
    zone_city,
)

# Load secrets from .env into environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")

if not TOKEN or not GUILD_ID or not os.getenv("OPENAI_API_KEY"):
    raise SystemExit("Missing DISCORD_TOKEN, DISCORD_GUILD_ID, or OPENAI_API_KEY in .env")
if not ALL_ZONES:
    raise SystemExit("No time zone data found. Run: pip install tzdata")

# The test server, as an object discord.py can sync commands to
TEST_GUILD = discord.Object(id=int(GUILD_ID))

MAX_MESSAGES = 200         # most messages /summarize will read at once
CATCHUP_CAP = 500          # most messages /catchup will read at once
FALLBACK_COUNT = 50        # used when SumUp can't tell when someone was last here
BIG_CATCHUP = 200          # above this many messages, allow more bullets
OWN_MESSAGE_GRACE = timedelta(minutes=10)  # ignore your own very recent messages
DISCORD_CHAR_LIMIT = 2000  # Discord's maximum message length


class SumUp(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed to read message text
        super().__init__(intents=intents)
        # The CommandTree holds all of the bot's slash commands
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Runs once at startup, before the bot connects.
        # Syncing to one server makes command changes appear instantly.
        self.tree.copy_global_to(guild=TEST_GUILD)
        synced = await self.tree.sync(guild=TEST_GUILD)
        print(f"Synced {len(synced)} command(s) to the test server")


client = SumUp()


# --- Helpers ------------------------------------------------------------------

def user_zone(user_id: int) -> Optional[ZoneInfo]:
    """The user's saved time zone, or None if they haven't set one."""
    zone = state.get_timezone(user_id)
    return ZoneInfo(zone) if zone else None


def format_message(message: discord.Message, tz: Optional[ZoneInfo]) -> Optional[str]:
    """Turn one Discord message into a transcript line, or None to skip it."""
    if message.author.bot:
        return None  # skip bots, including SumUp itself
    text = message.clean_content.strip()  # mentions become readable names
    for attachment in message.attachments:
        text += f" [attachment: {attachment.filename}]"
    text = text.strip()
    if not text:
        return None  # e.g. embed-only messages with no text
    if tz:
        time_label = format_clock(message.created_at.astimezone(tz))
    else:
        time_label = format_clock(message.created_at) + " UTC"  # created_at is already UTC
    return f"[{time_label}] {message.author.display_name}: {text}"


def split_message(text: str, limit: int = DISCORD_CHAR_LIMIT) -> list[str]:
    """Split text into chunks under Discord's limit, breaking at line ends."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)  # last line break that fits
        if cut <= 0:
            cut = limit  # one very long line: cut it mid-line
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def discord_time(dt: datetime) -> str:
    """Discord timestamp markup: each viewer sees it in their own local time."""
    return f"<t:{int(dt.timestamp())}:f>"


async def send_summary(interaction: discord.Interaction, header: str, lines: list[str]) -> bool:
    """Summarize transcript lines and send the result. Returns True on success."""
    max_bullets = 8 if len(lines) > BIG_CATCHUP else 6
    try:
        summary = await summarize_transcript("\n".join(lines), max_bullets=max_bullets)
    except openai.APIError as error:
        print(f"OpenAI error: {error!r}")  # full details for you, in the terminal
        await interaction.followup.send(
            "Sorry, I couldn't reach the summarizer right now. Try again in a moment.",
            ephemeral=True,
        )
        return False

    if not summary:
        await interaction.followup.send("The summarizer returned nothing. Try again.", ephemeral=True)
        return False

    for chunk in split_message(f"{header}\n{summary}"):
        await interaction.followup.send(chunk, ephemeral=True)
    return True


# --- Events -------------------------------------------------------------------

@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")


# --- Commands -----------------------------------------------------------------

@client.tree.command(name="ping", description="Check that SumUp is online")
async def ping(interaction: discord.Interaction):
    latency_ms = round(client.latency * 1000)
    await interaction.response.send_message(
        f"Σ Pong! Latency: {latency_ms} ms",
        ephemeral=True,  # "Only you can see this"
    )


@client.tree.command(name="summarize", description="Summarize recent messages in this channel")
@app_commands.describe(count=f"How many recent messages to read (1-{MAX_MESSAGES})")
async def summarize(
    interaction: discord.Interaction,
    count: app_commands.Range[int, 1, MAX_MESSAGES] = 50,
):
    # Reply within 3 seconds with a private "thinking..." placeholder
    await interaction.response.defer(ephemeral=True, thinking=True)
    tz = user_zone(interaction.user.id)

    try:
        # history() returns newest first
        messages = [m async for m in interaction.channel.history(limit=count)]
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to read this channel's history.", ephemeral=True
        )
        return

    lines = [line for m in reversed(messages) if (line := format_message(m, tz))]
    if not lines:
        await interaction.followup.send("No messages to summarize here.", ephemeral=True)
        return

    await send_summary(interaction, f"**Σ Summary of the last {len(lines)} messages**", lines)


@client.tree.command(name="catchup", description="Summarize what you missed in this channel")
@app_commands.describe(since="When you were last here, like 2h, 4:00 PM, or Sep 22 6:23 PM")
async def catchup(interaction: discord.Interaction, since: Optional[str] = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    now = discord.utils.utcnow()
    user = interaction.user
    channel = interaction.channel
    tz = user_zone(user.id)
    note = ""

    try:
        if since:
            # The user told us when they were last here
            try:
                start = parse_since(since, now, tz)
            except SinceError as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            # Newest first, stopping at the start time; one extra to detect the cap
            messages = [
                m async for m in channel.history(limit=CATCHUP_CAP + 1, after=start, oldest_first=False)
            ]
        else:
            # Work backwards until we reach the user's last /catchup here
            # or their last message (ignoring messages from the last few minutes,
            # like "what did I miss?")
            last_catchup = state.get_last_catchup(user.id, channel.id)
            start = None
            messages = []
            async for m in channel.history(limit=CATCHUP_CAP + 1):
                if last_catchup and m.created_at <= last_catchup:
                    start = last_catchup
                    break
                if m.author.id == user.id and m.created_at <= now - OWN_MESSAGE_GRACE:
                    start = m.created_at
                    break
                messages.append(m)

            if start is None and last_catchup is None:
                # No sign of when they were last here: fall back to recent messages
                messages = messages[:FALLBACK_COUNT]
                note = (
                    f"\n-# I couldn't tell when you were last here, so this covers the last "
                    f"{FALLBACK_COUNT} messages. Use `since` to pick a time."
                )
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to read this channel's history.", ephemeral=True
        )
        return

    if len(messages) > CATCHUP_CAP:
        messages = messages[:CATCHUP_CAP]
        note = (
            f"\n-# You missed more than {CATCHUP_CAP} messages, so this covers the most recent "
            f"{CATCHUP_CAP}."
        )

    lines = [line for m in reversed(messages) if (line := format_message(m, tz))]
    if not lines:
        state.set_last_catchup(user.id, channel.id, now)
        await interaction.followup.send("You're all caught up! No new messages.", ephemeral=True)
        return

    # If we didn't find a start point, the summary starts at the oldest message read
    shown_start = start if start and not note else messages[-1].created_at
    header = f"**Σ Catch-up: {len(lines)} messages since {discord_time(shown_start)}**{note}"
    if await send_summary(interaction, header, lines):
        state.set_last_catchup(user.id, channel.id, now)


@catchup.autocomplete("since")
async def since_autocomplete(interaction: discord.Interaction, current: str):
    """Show how SumUp reads the `since` text while the user types it."""
    if not current.strip():
        return [
            app_commands.Choice(name="Example: 2h (two hours ago)", value="2h"),
            app_commands.Choice(name="Example: 4:00 PM", value="4:00 PM"),
            app_commands.Choice(name="Example: Sep 22 6:23 PM", value="Sep 22 6:23 PM"),
        ]
    now = discord.utils.utcnow()
    tz = user_zone(interaction.user.id)
    try:
        label = "→ " + describe_since(parse_since(current, now, tz), now, tz)
    except SinceError as error:
        label = str(error)
    return [app_commands.Choice(name=label[:100], value=current[:100])]


@client.tree.command(name="timezone", description="Set your time zone so SumUp can read clock times")
@app_commands.describe(zone="Start typing your city, like Toronto")
async def set_timezone(interaction: discord.Interaction, zone: str):
    if zone not in ALL_ZONES:
        await interaction.response.send_message(
            "I don't recognize that time zone. Start typing a city and pick from the list.",
            ephemeral=True,
        )
        return
    state.set_timezone(interaction.user.id, zone)
    local_now = discord.utils.utcnow().astimezone(ZoneInfo(zone))
    await interaction.response.send_message(
        f"Time zone set to **{zone}**. It's {format_clock(local_now)} there right now.",
        ephemeral=True,
    )


@set_timezone.autocomplete("zone")
async def zone_autocomplete(interaction: discord.Interaction, current: str):
    now = discord.utils.utcnow()
    return [
        app_commands.Choice(
            name=f"{zone_city(zone)} ({zone}), now {format_clock(now.astimezone(ZoneInfo(zone)))}",
            value=zone,
        )
        for zone in search_zones(current)
    ]


client.run(TOKEN)
