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
from summarizer import DEFAULT_MODE, MODES, summarize
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

MESSAGE_CAP = 1000         # most messages /sumup will read at once
FIRST_TIME_CAP = 200       # messages covered on someone's first /sumup in a channel
BIG_SUMMARY = 200          # above this many messages, allow more bullets
BUTTON_MINUTES = 14        # buttons stop working just before Discord's 15-minute limit
CACHE_MINUTES = 60         # how long a summary is kept in memory for "Summarize again"
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
    return await saved_mode(user_id)


async def saved_mode(user_id: int) -> str:
    """The user's saved default mode. A mode that no longer exists
    (Brief and Action items were removed) falls back to the default."""
    mode = await state.get_mode(user_id)
    return mode if mode in MODES else DEFAULT_MODE


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


def range_note(window: Window, first_time: bool) -> str:
    """A short line under the header explaining the range, when it isn't obvious."""
    if first_time:
        covered = f"the last {FIRST_TIME_CAP} messages" if window.capped else "the whole channel"
        return (
            f"\n-# This is your first SumUp here, so this covers {covered}. "
            f"Next time it'll pick up where you left off. New to SumUp? Try `/help`."
        )
    if window.capped:
        return (
            f"\n-# You missed more than {MESSAGE_CAP:,} messages, so this covers the most "
            f"recent {MESSAGE_CAP:,}."
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


# --- Summaries and their buttons ---------------------------------------------

@dataclass
class SummaryRun:
    """One /sumup: the messages it covers, and every style written for it so far."""
    user_id: int
    channel: discord.abc.Messageable
    lines: list[str]               # transcript lines, oldest first
    shown_start: datetime          # the "since" time in the header
    note: str                      # the grey note under the header (private replies only)
    results: dict[str, str]        # mode -> summary text, so each style is written once
    shared: bool = False           # each summary can be shared publicly once
    last_mode: str = DEFAULT_MODE  # the style shown most recently

    def header(self, mode: str) -> str:
        return (
            f"**Σ SumUp ({MODES[mode]}): {len(self.lines)} messages since "
            f"{discord_time(self.shown_start)}**{self.note}"
        )

    async def write(self, mode: str) -> str:
        """Return this run's summary in `mode`, calling the model only the first time."""
        if mode not in self.results:
            max_bullets = 8 if len(self.lines) > BIG_SUMMARY else 6
            summary = await summarize(self.lines, mode=mode, max_bullets=max_bullets)
            if not summary:
                raise EmptySummary()
            self.results[mode] = summary
        return self.results[mode]


class EmptySummary(Exception):
    """The model returned no text."""


SUMMARIZER_DOWN = "Sorry, I couldn't reach the summarizer right now. Try again in a moment."
SUMMARIZER_EMPTY = "The summarizer returned nothing. Try again."


async def send_private(interaction: discord.Interaction, run: SummaryRun, mode: str) -> None:
    """Send the run's summary privately, with the buttons on the last message."""
    run.last_mode = mode
    chunks = split_message(f"{run.header(mode)}\n{run.results[mode]}")
    for chunk in chunks[:-1]:
        await interaction.followup.send(chunk, ephemeral=True)
    view = SummaryView(run, mode)
    view.message = await interaction.followup.send(chunks[-1], ephemeral=True, view=view, wait=True)


class SummaryView(discord.ui.View):
    """Share to channel and Switch style buttons under a private summary."""

    def __init__(self, run: SummaryRun, mode: str):
        super().__init__(timeout=BUTTON_MINUTES * 60)
        self.run = run
        self.mode = mode
        self.message: Optional[discord.WebhookMessage] = None
        self.other = next(m for m in MODES if m != mode)

        self.share_button.disabled = run.shared
        if run.shared:
            self.share_button.label = "Shared"
        self.switch_button.label = f"Switch to {MODES[self.other]}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.run.user_id

    @discord.ui.button(label="Share to channel", emoji="📢", style=discord.ButtonStyle.primary)
    async def share_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        name = discord.utils.escape_markdown(interaction.user.display_name)
        public = (
            f"**Σ SumUp ({MODES[self.mode]}), shared by {name}: {len(self.run.lines)} messages "
            f"since {discord_time(self.run.shown_start)}**\n{self.run.results[self.mode]}"
        )
        try:
            for chunk in split_message(public):
                # AllowedMentions.none(): nothing in a shared summary can ping anyone
                await self.run.channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to post in this channel.", ephemeral=True
            )
            return
        self.run.shared = True
        button.disabled = True
        button.label = "Shared"
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Switch", style=discord.ButtonStyle.secondary)
    async def switch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        target = self.other
        is_new = target not in self.run.results
        now = discord.utils.utcnow()

        if is_new and await state.get_usage(self.run.user_id, limit_day(now)) >= DAILY_LIMIT:
            reset = discord_time(next_reset(now), "R")
            await interaction.response.send_message(
                f"You've used all {DAILY_LIMIT} SumUps for today. Your limit resets {reset}.",
                ephemeral=True,
            )
            return

        # Remove these buttons while the other style is written, and say what's happening
        original = interaction.message.content if interaction.message else ""
        await interaction.response.edit_message(
            content=f"{original}\n-# Writing the {MODES[target]} version…", view=None
        )
        try:
            await self.run.write(target)
        except (openai.APIError, EmptySummary) as error:
            print(f"Switch failed: {error!r}")
            await interaction.edit_original_response(content=original, view=self)
            text = SUMMARIZER_EMPTY if isinstance(error, EmptySummary) else SUMMARIZER_DOWN
            await interaction.followup.send(text, ephemeral=True)
            return

        if is_new:
            await state.add_usage(self.run.user_id, limit_day(now))
        await interaction.edit_original_response(content=original)
        self.stop()
        await send_private(interaction, self.run, target)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass  # too late to edit; Discord shows the buttons as expired


# --- Short-term memory of recent summaries -------------------------------------

# (user ID, channel ID) -> (expiry as time.monotonic() seconds, the summary run).
# Memory only, never the database: it holds conversation content, so it expires
# after CACHE_MINUTES, disappears on restart, and /forget clears it.
_recent: dict[tuple[int, int], tuple[float, SummaryRun]] = {}


def remember(run: SummaryRun, channel_id: int) -> None:
    now = time.monotonic()
    for key in [k for k, (expires, _) in _recent.items() if expires <= now]:
        del _recent[key]  # drop anything expired while we're here
    _recent[(run.user_id, channel_id)] = (now + CACHE_MINUTES * 60, run)


def recall(user_id: int, channel_id: int) -> Optional[SummaryRun]:
    entry = _recent.get((user_id, channel_id))
    if entry is None or entry[0] <= time.monotonic():
        _recent.pop((user_id, channel_id), None)
        return None
    return entry[1]


def forget_recent(user_id: int) -> None:
    for key in [k for k in _recent if k[0] == user_id]:
        del _recent[key]


REPEAT_NOTE = "\n-# A repeat of your last SumUp here."


class AgainView(discord.ui.View):
    """The "Summarize again" button on an "all caught up" reply."""

    def __init__(self, user_id: int):
        super().__init__(timeout=BUTTON_MINUTES * 60)
        self.user_id = user_id
        self.message: Optional[discord.WebhookMessage] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Summarize again", emoji="🔁", style=discord.ButtonStyle.primary)
    async def again_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        channel = interaction.channel

        # Fast path: the last summary is still in memory, so show it again for free
        run = recall(user.id, channel.id)
        if run is not None:
            run.note = REPEAT_NOTE
            await interaction.response.edit_message(view=None)
            self.stop()
            await send_private(interaction, run, run.last_mode)
            return

        # Otherwise write it again from the same messages, which counts as a SumUp
        now = discord.utils.utcnow()
        wait_message = await check_limits(user.id, now)
        if wait_message:
            await interaction.response.send_message(wait_message, ephemeral=True)
            return
        last_range = await state.get_last_range(user.id, channel.id)
        if last_range is None:
            await interaction.response.send_message(
                "I don't have a previous SumUp to repeat here. Try `/sumup since: 1h`.", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            content=f"{interaction.message.content}\n-# Summarizing again…", view=None
        )
        self.stop()
        _cooldowns[user.id] = time.monotonic()

        start, end = last_range
        tz = await user_zone(user.id)
        # 1 ms earlier so the first message of the original summary is included
        messages = [
            m async for m in channel.history(
                limit=MESSAGE_CAP, after=start - timedelta(milliseconds=1), before=end, oldest_first=True
            )
        ]
        lines = [line for m in messages if (line := format_message(m, tz))]
        if not lines:
            _cooldowns.pop(user.id, None)
            await interaction.followup.send("Those messages aren't available anymore.", ephemeral=True)
            return

        run = SummaryRun(
            user_id=user.id, channel=channel, lines=lines, shown_start=start,
            note=REPEAT_NOTE, results={},
        )
        mode = await saved_mode(user.id)
        try:
            await run.write(mode)
        except (openai.APIError, EmptySummary) as error:
            print(f"Repeat failed: {error!r}")
            _cooldowns.pop(user.id, None)
            text = SUMMARIZER_EMPTY if isinstance(error, EmptySummary) else SUMMARIZER_DOWN
            await interaction.followup.send(text, ephemeral=True)
            return

        await state.add_usage(user.id, limit_day(now))
        remember(run, channel.id)
        await send_private(interaction, run, mode)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


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

    # First /sumup in this channel with no earlier message of yours found:
    # cover just the most recent messages, as background
    first_time = not since and not last_sumup and window.start is None
    if first_time and len(window.messages) > FIRST_TIME_CAP:
        window = Window(window.messages[:FIRST_TIME_CAP], None, True)

    lines = [line for m in reversed(window.messages) if (line := format_message(m, tz))]
    if not lines:
        _cooldowns.pop(user.id, None)  # nothing was summarized, so no cooldown
        has_previous = (
            not since
            and (recall(user.id, channel.id) or await state.get_last_range(user.id, channel.id))
        )
        if has_previous:
            view = AgainView(user.id)
            view.message = await interaction.followup.send(
                "You're all caught up! No new messages since your last SumUp.",
                ephemeral=True, view=view, wait=True,
            )
        else:
            await interaction.followup.send("You're all caught up! No new messages.", ephemeral=True)
        return

    # Without a known start point, the summary starts at the oldest message read
    if window.start and not window.capped:
        shown_start = window.start
    else:
        shown_start = window.messages[-1].created_at

    mode_key = await resolve_mode(user.id, mode)
    run = SummaryRun(
        user_id=user.id,
        channel=channel,
        lines=lines,
        shown_start=shown_start,
        note=range_note(window, first_time),
        results={},
    )
    try:
        await run.write(mode_key)
    except (openai.APIError, EmptySummary) as error:
        print(f"Summary failed: {error!r}")
        _cooldowns.pop(user.id, None)  # the summarizer failed, so let them retry
        text = SUMMARIZER_EMPTY if isinstance(error, EmptySummary) else SUMMARIZER_DOWN
        await interaction.followup.send(text, ephemeral=True)
        return

    first_covered = window.messages[-1].created_at  # oldest message this summary read
    await state.set_last_sumup(user.id, channel.id, now, first_covered)
    await state.add_usage(user.id, limit_day(now))
    remember(run, channel.id)
    await send_private(interaction, run, mode_key)


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


HELP_TEXT = f"""**Σ SumUp** catches you up on conversations you missed. Every reply is private unless you share it.

**Commands**
`/sumup`: summarize what you missed in this channel
- Starts from your last `/sumup` here or your last message, whichever is more recent
- First time in a channel: covers the last {FIRST_TIME_CAP} messages
- `since`: start from a time instead, like `2h`, `30m`, `4pm`, `yesterday 9pm`, or `Sep 22 6:23 PM`
- `mode`: **Bullets** (quick points) or **Detailed** (by topic, with to-dos and open questions)
`/mode`: set your default style
`/timezone`: set your time zone so times like `4pm` mean your 4 PM
`/forget`: delete everything SumUp has saved about you
`/help`: show this message

**Under each summary**
- **Share to channel** posts it publicly so everyone can see it
- **Switch to Detailed / Bullets** rewrites the same messages in the other style
- Buttons work for about {BUTTON_MINUTES} minutes
- Dismissed a summary? Run `/sumup` again and press **Summarize again** to get it back

**Limits**
- One `/sumup` every {COOLDOWN_SECONDS} seconds, {DAILY_LIMIT} a day (resets at midnight Eastern)
- Up to {MESSAGE_CAP:,} messages per summary
-# SumUp never stores message text. Summaries are written by OpenAI."""


@client.tree.command(name="help", description="What SumUp does and how to use it")
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(HELP_TEXT, ephemeral=True)


@client.tree.command(name="mode", description="Set your default summary style")
@app_commands.describe(mode="Your default style (leave empty to see your current one)")
@app_commands.choices(mode=MODE_CHOICES)
async def set_mode(
    interaction: discord.Interaction,
    mode: Optional[app_commands.Choice[str]] = None,
):
    if mode is None:
        current = MODES[await saved_mode(interaction.user.id)]
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
        "This deletes your time zone, default mode, where each of your SumUps "
        "left off in every channel, and any recent summaries SumUp is holding for "
        "Summarize again. Your next SumUp will start fresh.\n"
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
        forget_recent(self.user_id)
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
