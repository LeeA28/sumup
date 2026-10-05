import os

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


class SumUp(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed later to read message text
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


client.run(TOKEN)
