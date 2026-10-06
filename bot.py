import os
from typing import Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

# Load secrets from .env into environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")

if not TOKEN or not GUILD_ID:
    raise SystemExit("Missing DISCORD_TOKEN or DISCORD_GUILD_ID in .env")

# The test server, as an object discord.py can sync commands to
TEST_GUILD = discord.Object(id=int(GUILD_ID))

MAX_MESSAGES = 200      # most messages /summarize will read at once
PREVIEW_LIMIT = 1800    # Discord messages max out at 2000 characters


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

    # Step 2 shows the raw transcript; Step 3 will send it to the LLM instead
    if len(transcript) > PREVIEW_LIMIT:
        transcript = "...\n" + transcript[-PREVIEW_LIMIT:]
    await interaction.followup.send(
        f"Read {len(lines)} messages:\n```\n{transcript}\n```",
        ephemeral=True,
    )


client.run(TOKEN)
