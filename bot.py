import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
import openai
from discord import app_commands
from dotenv import load_dotenv

import state
from summarizer import DEFAULT_MODE, MODES, summarize_transcript
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

MESSAGE_CAP = 500          # most messages /sumup will read at once
BIG_SUMMARY = 200          # above this many messages, allow more bullets
OWN_MESSAGE_GRACE = timedelta(minutes=10)  # ignore your own very recent messages
DISCORD_CHAR_LIMIT = 2000  # Discord's maximum message length

# Dropdown options for picking a mode: label shown to users, key used in code
MODE_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in MODES.items()]


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

def resolve_mode(user_id: int, choice: Optional[app_commands.Choice[str]]) -> str:
    """The mode picked for this command, else the user's default, else bullets."""
    if choice:
        return choice.value
    return state.get_mode(user_id) or DEFAULT_MODE


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


async def send_summary(
    interaction: discord.Interaction, header: str, lines: list[str], mode: str
) -> bool:
    """Summarize transcript lines and send the result. Returns True on success."""
    max_bullets = 8 if len(lines) > BIG_SUMMARY else 6
    try:
        summary = await summarize_transcript("\n".join(lines), mode=mode, max_bullets=max_bullets)
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


@client.tree.command(name="sumup", description="Summarize what you missed in this channel")
@app_commands.describe(
    since="When you were last here, like 2h, 4:00 PM, or Sep 22 6:23 PM",
    mode="Summary style for this one summary (default: your /mode setting)",
)
@app_commands.choices(mode=MODE_CHOICES)
async def sumup(
    interaction: discord.Interaction,
    since: Optional[str] = None,
    mode: Optional[app_commands.Choice[str]] = None,
):
    # Reply within 3 seconds with a private "thinking..." placeholder
    await interaction.response.defer(ephemeral=True, thinking=True)
    now = discord.utils.utcnow()
    user = interaction.user
    channel = interaction.channel
    tz = user_zone(user.id)
    last_sumup = state.get_last_sumup(user.id, channel.id)
    start = None

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
                m async for m in channel.history(limit=MESSAGE_CAP + 1, after=start, oldest_first=False)
            ]
        else:
            # Work backwards until we reach the user's last /sumup here
            # or their last message (ignoring messages from the last few minutes,
            # like "what did I miss?")
            messages = []
            async for m in channel.history(limit=MESSAGE_CAP + 1):
                if last_sumup and m.created_at <= last_sumup:
                    start = last_sumup
                    break
                if m.author.id == user.id and m.created_at <= now - OWN_MESSAGE_GRACE:
                    start = m.created_at
                    break
                messages.append(m)
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to read this channel's history.", ephemeral=True
        )
        return

    # Explain to the user what range this summary covers, when it isn't obvious
    note = ""
    capped = len(messages) > MESSAGE_CAP
    if capped:
        messages = messages[:MESSAGE_CAP]
        if since or last_sumup:
            note = (
                f"\n-# You missed more than {MESSAGE_CAP} messages, so this covers the most "
                f"recent {MESSAGE_CAP}."
            )
        else:
            note = (
                f"\n-# This is your first SumUp here, so this covers the last {MESSAGE_CAP} "
                f"messages. Next time it'll pick up where you left off."
            )
    elif start is None and not last_sumup:
        note = (
            "\n-# This is your first SumUp here, so this covers the whole channel. "
            "Next time it'll pick up where you left off."
        )

    lines = [line for m in reversed(messages) if (line := format_message(m, tz))]
    if not lines:
        state.set_last_sumup(user.id, channel.id, now)
        await interaction.followup.send("You're all caught up! No new messages.", ephemeral=True)
        return

    # Without a known start point, the summary starts at the oldest message read
    shown_start = start if start and not capped else messages[-1].created_at
    mode_key = resolve_mode(user.id, mode)
    header = (
        f"**Σ SumUp ({MODES[mode_key]}): {len(lines)} messages since "
        f"{discord_time(shown_start)}**{note}"
    )
    if await send_summary(interaction, header, lines, mode_key):
        state.set_last_sumup(user.id, channel.id, now)


@sumup.autocomplete("since")
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


@client.tree.command(name="mode", description="Set your default summary style")
@app_commands.describe(mode="Your default style (leave empty to see your current one)")
@app_commands.choices(mode=MODE_CHOICES)
async def set_mode(
    interaction: discord.Interaction,
    mode: Optional[app_commands.Choice[str]] = None,
):
    if mode is None:
        current = MODES[state.get_mode(interaction.user.id) or DEFAULT_MODE]
        await interaction.response.send_message(
            f"Your default mode is **{current}**. Pick a mode in this command to change it.",
            ephemeral=True,
        )
        return
    state.set_mode(interaction.user.id, mode.value)
    await interaction.response.send_message(
        f"Your default mode is now **{mode.name}**. You can still pick a different mode "
        f"for one summary with the `mode` option.",
        ephemeral=True,
    )


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
