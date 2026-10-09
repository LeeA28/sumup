import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
import openai
from discord import app_commands
from discord.ext import tasks
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

# Load secrets from .env into environment variables.
# On Railway there's no .env file; the same names are set as environment variables.
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # optional: set for development only

if not TOKEN or not os.getenv("OPENAI_API_KEY") or not DATABASE_URL:
    raise SystemExit("Missing DISCORD_TOKEN, OPENAI_API_KEY, or DATABASE_URL")
if not ALL_ZONES:
    raise SystemExit("No time zone data found. Run: pip install tzdata")

# The test server, if one is set
TEST_GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

MESSAGE_CAP = 500          # most messages /sumup will read at once
BIG_SUMMARY = 200          # above this many messages, allow more bullets
OWN_MESSAGE_GRACE = timedelta(minutes=10)  # ignore your own very recent messages
DISCORD_CHAR_LIMIT = 2000  # Discord's maximum message length
COOLDOWN_SECONDS = 30      # wait between /sumup runs, per user
DAILY_LIMIT = 50           # successful summaries per user per day
LIMIT_ZONE = ZoneInfo("America/Toronto")  # the daily limit resets at midnight here
USAGE_KEEP_DAYS = 3        # how long old daily-usage rows are kept

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
        # Connect to the database first, so commands can use it right away.
        await state.init(DATABASE_URL)
        print("Connected to the database")
        prune_old_usage.start()

        if TEST_GUILD:
            # Development: sync to one server, which updates instantly
            self.tree.copy_global_to(guild=TEST_GUILD)
            synced = await self.tree.sync(guild=TEST_GUILD)
            print(f"Synced {len(synced)} command(s) to the test server")
        else:
            # Production: sync everywhere (can take up to an hour to appear)
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} command(s) globally")

    async def close(self):
        # Runs on shutdown (like Ctrl + C): close database connections cleanly
        prune_old_usage.cancel()
        await state.close()
        await super().close()


client = SumUp()


# --- Helpers ------------------------------------------------------------------

async def resolve_mode(user_id: int, choice: Optional[app_commands.Choice[str]]) -> str:
    """The mode picked for this command, else the user's default, else bullets."""
    if choice:
        return choice.value
    return await state.get_mode(user_id) or DEFAULT_MODE


async def user_zone(user_id: int) -> Optional[ZoneInfo]:
    """The user's saved time zone, or None if they haven't set one."""
    zone = await state.get_timezone(user_id)
    return ZoneInfo(zone) if zone else None


# --- Limits -------------------------------------------------------------------

# user ID -> when their cooldown started (time.monotonic() seconds).
# Kept in memory: a restart clearing a 30-second cooldown doesn't matter.
_cooldowns: dict[int, float] = {}


def limit_day(now: datetime):
    """Which day a moment counts toward for the daily limit (the Toronto date)."""
    return now.astimezone(LIMIT_ZONE).date()


def next_reset(now: datetime) -> datetime:
    """The next midnight in Toronto, when the daily limit resets.
    Built from the date, so daylight saving changes are handled by zoneinfo."""
    tomorrow = limit_day(now) + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time(), tzinfo=LIMIT_ZONE)


async def check_limits(user_id: int, now: datetime) -> Optional[str]:
    """Return a message if the user has to wait, or None if they can go ahead."""
    started = _cooldowns.get(user_id)
    if started is not None:
        remaining = COOLDOWN_SECONDS - (time.monotonic() - started)
        if remaining > 0:
            return f"Slow down a little! Try again in {int(remaining) + 1} seconds."

    used = await state.get_usage(user_id, limit_day(now))
    if used >= DAILY_LIMIT:
        reset = discord_time(next_reset(now), "R")
        return f"You've used all {DAILY_LIMIT} SumUps for today. Your limit resets {reset}."
    return None


@tasks.loop(hours=24)
async def prune_old_usage():
    """Once a day (and at startup), delete daily-usage rows older than a few days."""
    today = limit_day(discord.utils.utcnow())
    await state.prune_usage(today - timedelta(days=USAGE_KEEP_DAYS))


# --- Finding what to summarize ------------------------------------------------

@dataclass
class Window:
    """The messages a /sumup covers, and how that range was chosen."""
    messages: list[discord.Message]  # newest first
    start: Optional[datetime]        # known start point, if any
    capped: bool                     # True if there were more than MESSAGE_CAP


async def fetch_since(channel, start: datetime) -> Window:
    """Messages after a time the user gave with `since`."""
    messages = [
        m async for m in channel.history(limit=MESSAGE_CAP + 1, after=start, oldest_first=False)
    ]
    return Window(messages[:MESSAGE_CAP], start, len(messages) > MESSAGE_CAP)


async def fetch_missed(channel, user, now: datetime, last_sumup: Optional[datetime]) -> Window:
    """Work backwards until we reach the user's last /sumup here or their
    last message (ignoring their own messages from the last few minutes,
    like "what did I miss?")."""
    messages = []
    start = None
    async for m in channel.history(limit=MESSAGE_CAP + 1):
        if last_sumup and m.created_at <= last_sumup:
            start = last_sumup
            break
        if m.author.id == user.id and m.created_at <= now - OWN_MESSAGE_GRACE:
            start = m.created_at
            break
        messages.append(m)
    return Window(messages[:MESSAGE_CAP], start, len(messages) > MESSAGE_CAP)


def range_note(window: Window, used_since: bool, last_sumup: Optional[datetime]) -> str:
    """A short line under the header explaining the range, when it isn't obvious."""
    first_time = not used_since and not last_sumup
    if window.capped:
        if first_time:
            return (
                f"\n-# This is your first SumUp here, so this covers the last {MESSAGE_CAP} "
                f"messages. Next time it'll pick up where you left off."
            )
        return (
            f"\n-# You missed more than {MESSAGE_CAP} messages, so this covers the most "
            f"recent {MESSAGE_CAP}."
        )
    if window.start is None and first_time:
        return (
            "\n-# This is your first SumUp here, so this covers the whole channel. "
            "Next time it'll pick up where you left off."
        )
    return ""


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


def discord_time(dt: datetime, style: str = "f") -> str:
    """Discord timestamp markup: each viewer sees it in their own local time.
    Style "f" shows a date and time; "R" shows relative time like "in 3 hours"."""
    return f"<t:{int(dt.timestamp())}:{style}>"


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


# --- Events and errors ----------------------------------------------------------

@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")


@client.tree.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Last line of defense: any error a command didn't handle itself
    (like the database being unreachable) gets a friendly reply instead of silence."""
    print(f"Error in /{interaction.command.name if interaction.command else '?'}: {error!r}")
    if isinstance(error, app_commands.NoPrivateMessage):
        text = "SumUp only works in servers, not in DMs."
    else:
        text = "Something went wrong on my end. Please try again in a moment."
    if interaction.command and interaction.command.name == "sumup":
        _cooldowns.pop(interaction.user.id, None)  # a failed run shouldn't cost a cooldown
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass  # the interaction expired; nothing more we can do


# --- Commands -----------------------------------------------------------------

@client.tree.command(name="ping", description="Check that SumUp is online")
async def ping(interaction: discord.Interaction):
    latency_ms = round(client.latency * 1000)
    await interaction.response.send_message(
        f"Σ Pong! Latency: {latency_ms} ms",
        ephemeral=True,  # "Only you can see this"
    )


@client.tree.command(name="sumup", description="Summarize what you missed in this channel")
@app_commands.guild_only()
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

    wait_message = await check_limits(user.id, now)
    if wait_message:
        await interaction.followup.send(wait_message, ephemeral=True)
        return
    _cooldowns[user.id] = time.monotonic()  # start now, so double-clicks are blocked

    tz = await user_zone(user.id)
    last_sumup = await state.get_last_sumup(user.id, channel.id)

    if since:
        try:
            start = parse_since(since, now, tz)
        except SinceError as error:
            _cooldowns.pop(user.id, None)  # a typo shouldn't cost a cooldown
            await interaction.followup.send(str(error), ephemeral=True)
            return

    try:
        if since:
            window = await fetch_since(channel, start)
        else:
            window = await fetch_missed(channel, user, now, last_sumup)
    except discord.Forbidden:
        _cooldowns.pop(user.id, None)
        await interaction.followup.send(
            "I don't have permission to read this channel's history.", ephemeral=True
        )
        return

    lines = [line for m in reversed(window.messages) if (line := format_message(m, tz))]
    if not lines:
        _cooldowns.pop(user.id, None)  # nothing was summarized, so no cooldown
        await state.set_last_sumup(user.id, channel.id, now)
        await interaction.followup.send("You're all caught up! No new messages.", ephemeral=True)
        return

    # Without a known start point, the summary starts at the oldest message read
    if window.start and not window.capped:
        shown_start = window.start
    else:
        shown_start = window.messages[-1].created_at
    mode_key = await resolve_mode(user.id, mode)
    header = (
        f"**Σ SumUp ({MODES[mode_key]}): {len(lines)} messages since "
        f"{discord_time(shown_start)}**{range_note(window, bool(since), last_sumup)}"
    )
    if await send_summary(interaction, header, lines, mode_key):
        await state.set_last_sumup(user.id, channel.id, now)
        await state.add_usage(user.id, limit_day(now))
    else:
        _cooldowns.pop(user.id, None)  # the summarizer failed, so let them retry


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
    tz = await user_zone(interaction.user.id)
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
        current = MODES[await state.get_mode(interaction.user.id) or DEFAULT_MODE]
        await interaction.response.send_message(
            f"Your default mode is **{current}**. Pick a mode in this command to change it.",
            ephemeral=True,
        )
        return
    await state.set_mode(interaction.user.id, mode.value)
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
    await state.set_timezone(interaction.user.id, zone)
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


@client.tree.command(name="forget", description="Delete everything SumUp has saved about you")
async def forget(interaction: discord.Interaction):
    view = ForgetConfirm(interaction.user.id)
    await interaction.response.send_message(
        "This deletes your time zone, default mode, and where each of your SumUps "
        "left off in every channel. Your next SumUp will start fresh.\n"
        "-# Your daily SumUp count isn't reset, so the daily limit stays fair.",
        view=view,
        ephemeral=True,
    )
    view.message_interaction = interaction


class ForgetConfirm(discord.ui.View):
    """Confirm / Cancel buttons for /forget."""

    def __init__(self, user_id: int):
        super().__init__(timeout=60)  # buttons stop working after 60 seconds
        self.user_id = user_id
        self.message_interaction: Optional[discord.Interaction] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only the person who ran /forget can press the buttons
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Delete my data", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await state.forget_user(self.user_id)
        self.stop()
        await interaction.response.edit_message(
            content="Done. Everything SumUp saved about you has been deleted.", view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was deleted.", view=None)

    async def on_timeout(self):
        if self.message_interaction:
            try:
                await self.message_interaction.edit_original_response(
                    content="Timed out. Nothing was deleted. Run /forget again if you still want to.",
                    view=None,
                )
            except discord.HTTPException:
                pass


client.run(TOKEN)
