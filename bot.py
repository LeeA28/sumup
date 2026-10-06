import os
from typing import Optional

import discord
import openai
from discord import app_commands
from dotenv import load_dotenv

from summarizer import summarize_transcript

# Load secrets from .env into environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")

if not TOKEN or not GUILD_ID or not os.getenv("OPENAI_API_KEY"):
    raise SystemExit("Missing DISCORD_TOKEN, DISCORD_GUILD_ID, or OPENAI_API_KEY in .env")

# The test server, as an object discord.py can sync commands to
TEST_GUILD = discord.Object(id=int(GUILD_ID))

MAX_MESSAGES = 200        # most messages /summarize will read at once
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


def format_message(message: discord.Message) -> Optional[str]:
    """Turn one Discord message into a transcript line, or None to skip it."""
    text = message.clean_content.strip()  # mentions become readable names
    for attachment in message.attachments:
        text += f" [attachment: {attachment.filename}]"
    text = text.strip()
    if not text:
        return None  # e.g. embed-only messages with no text
    time = message.created_at.astimezone().strftime("%I:%M %p")
    return f"[{time}] {message.author.display_name}: {text}"


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


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")


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

    lines = []
    try:
        # history() returns newest first
        async for message in interaction.channel.history(limit=count):
            if message.author.bot:
                continue  # skip bots, including SumUp itself
            line = format_message(message)
            if line:
                lines.append(line)
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to read this channel's history.",
            ephemeral=True,
        )
        return

    if not lines:
        await interaction.followup.send("No messages to summarize here.", ephemeral=True)
        return

    lines.reverse()  # oldest first, the way people read a chat
    transcript = "\n".join(lines)

    try:
        summary = await summarize_transcript(transcript)
    except openai.APIError as error:
        print(f"OpenAI error: {error!r}")  # full details for you, in the terminal
        await interaction.followup.send(
            "Sorry, I couldn't reach the summarizer right now. Try again in a moment.",
            ephemeral=True,
        )
        return

    if not summary:
        await interaction.followup.send("The summarizer returned nothing. Try again.", ephemeral=True)
        return

    reply = f"**Σ Summary of the last {len(lines)} messages**\n{summary}"
    for chunk in split_message(reply):
        await interaction.followup.send(chunk, ephemeral=True)


client.run(TOKEN)
