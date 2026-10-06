# SumUp Build Guide

A record of every step, decision, and concept in building SumUp, a Discord bot that summarizes conversations with an LLM.

## Tech stack

- **Language:** Python
- **Discord library:** discord.py
- **LLM:** OpenAI API, model GPT-6 Luna, reasoning effort low
- **Secrets:** python-dotenv with a gitignored `.env` file
- **Hosting (planned):** Railway

## Key decisions

### Why OpenAI GPT-6 Luna

- Tested GPT-6 Luna (OpenAI Playground) against Claude Haiku 4.5 (Claude Console), the two providers' small, low-cost models.
- Test input: a deliberately messy fake Discord chat with a date change, tangents, a vague message ("there's cheaper"), a settled question (the speaker), and a trap detail (the hot tub was $50 *last time*).
- Same system prompt for both, 3 runs each.
- Results:
  - Luna: accurate in all 3 runs; nothing invented.
  - Haiku: one factual error in each run (misreading the speaker question, stating the hot tub costs $50, misstating who Jordan rides with).
- Cost per summary: Luna about $0.00012 vs Haiku about $0.0013, roughly 10x cheaper.
- Lowering Luna's reasoning effort from medium to low cut output tokens from 254 to about 140 with no quality loss.

### All replies are ephemeral

- Every SumUp reply is visible only to the person who ran the command ("Only you can see this").
- Ephemeral messages only work as responses to slash commands, so all input to SumUp goes through slash commands, never regular messages.
- Ephemeral messages disappear when dismissed or when Discord reloads, which suits catch-up summaries.

### Name and avatar

- Kept the name SumUp because it says what the bot does at a glance.
- The avatar is a Σ (summation symbol): the bot "sums up" the chat.

## Step 0: Setup

### Discord application

- Created the SumUp application in the Discord Developer Portal.
- Enabled the **Message Content** privileged intent. Without it, the bot receives messages with empty text.
- Invited the bot with the `bot` and `applications.commands` scopes and the View Channels, Send Messages, Read Message History, and Embed Links permissions.

### API key handling

- Dev key (`sumup-dev`) with a 30-day expiration, so a leaked key stops working on its own.
- A separate production key will be created for Railway.
- Industry practices followed:
  - Secrets never go in code; they live in `.env` locally and in Railway environment variables in production.
  - Separate keys per environment, so one can be revoked without breaking the other.
  - Spending limit set on the OpenAI account.
  - If a key leaks: revoke it first, create a new one, check usage logs, then clean up Git history.

### Project setup

- Repo created on GitHub with the Python `.gitignore` template, which already ignores `.env` and `.venv/`.
- Virtual environment (`.venv`) keeps SumUp's packages separate from other projects.
- Packages: `discord.py`, `openai`, `python-dotenv`, recorded in `requirements.txt` with `pip freeze`.
- `.env` holds `DISCORD_TOKEN`, `OPENAI_API_KEY`, and `DISCORD_GUILD_ID`.

### Git concepts

- `git status` compares the working folder, the staging area, and the last commit.
  - Red files are not staged: new untracked files, or tracked files changed but not added.
  - Green files are staged and will go into the next commit.
- `git add` stages files, `git commit` saves a snapshot, and `git push` uploads it to GitHub.

## Step 1: Hello bot

### What it does

- SumUp comes online and responds to `/ping` with an ephemeral reply showing its latency.

### How `bot.py` works

- **Loading secrets:** `load_dotenv()` reads `.env` into environment variables, and `os.getenv()` reads them. The bot stops with a clear error if either value is missing.
- **Intents:** Discord only sends a bot the event types it asks for. `Intents.default()` covers the basics, and `message_content = True` adds message text, which the summarizing features will need.
- **Client and CommandTree:** `discord.Client` is the connection to Discord. The `CommandTree` holds all slash commands and registers them with Discord.
- **`setup_hook`:** runs once at startup, before connecting. It syncs commands to the test server only.
  - Syncing to one server makes changes appear instantly. Global syncing can take up to an hour to show up, which makes testing slow.
- **`on_ready`:** an event that fires once the bot is connected. It prints confirmation to the terminal.
- **`/ping`:**
  - `@client.tree.command(...)` registers the function as a slash command with a name and description.
  - `client.latency` is the delay between the bot and Discord in seconds, converted to milliseconds.
  - `ephemeral=True` makes the reply visible only to the user who ran it.
- **`client.run(TOKEN)`:** logs in and keeps the bot running until stopped.

## Step 2: Reading history

### What it does

- `/summarize` reads recent messages in the channel and shows them back as a transcript, visible only to the person who ran it.
- No AI yet. This step shows exactly what data the bot will later send to the LLM.

### How it works

- **The `count` option:**
  - `app_commands.Range[int, 1, 200]` makes Discord itself enforce the range in the command menu, so the bot never receives an invalid number.
  - Defaults to 50 if the user leaves it blank.
- **Deferring:**
  - Discord requires a response to a slash command within 3 seconds.
  - `interaction.response.defer(ephemeral=True, thinking=True)` immediately shows a private "SumUp is thinking..." placeholder.
  - The real answer is sent later with `interaction.followup.send(...)`, which is allowed for up to 15 minutes.
- **Reading messages:**
  - `channel.history(limit=count)` returns messages newest first, so the list is reversed afterwards to read oldest first, like a normal chat.
  - It is an async iterator (`async for`): messages are fetched from Discord in batches over the network, so the bot awaits each batch instead of freezing.
  - `limit` counts every message, including skipped ones, so the transcript can have fewer lines than `count`.
- **Filtering (`format_message`):**
  - Skips bots, including SumUp itself, so summaries never include bot output.
  - `clean_content` replaces raw mention codes like `<@123456>` with readable names.
  - Attachments are kept as `[attachment: filename]` so the summary knows something was shared.
  - Messages with no text at all (for example embed-only messages) are skipped.
  - Each line is formatted as `[time] name: text`, the same format used in the API test.
- **Error handling:** if the bot lacks permission to read the channel, Discord raises `discord.Forbidden`, and the bot replies with a clear message instead of crashing.
- **Length limit:** Discord messages max out at 2000 characters, so the preview shows only the most recent part of long transcripts. In Step 3 the full transcript goes to the LLM instead.

### Known limitation

- Times use the computer's local time zone (`astimezone()`). On Railway, servers run in UTC, so this will need revisiting before deployment.
