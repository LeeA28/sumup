# Σ SumUp

A Discord bot that tells you what you missed. Run `/sumup` in any channel and get a private, AI-written summary of everything since you were last there: decisions, who's doing what, and questions still open.

Built with Python, discord.py, the OpenAI API (GPT-6 Luna), and PostgreSQL. Deployed on Railway.

## Commands

| Command | What it does |
|---|---|
| `/sumup` | Summarizes what you missed in this channel. Picks up from your last `/sumup` or your last message, whichever is more recent. New here? You get the last 500 messages. |
| `/sumup since:2h` | Summarizes from a time you choose: `2h`, `30m`, `4:00 PM`, `yesterday 9pm`, or `Sep 22 6:23 PM` (the format of Discord's "new messages since" banner). A live preview shows how SumUp reads your input as you type. |
| `/sumup mode:` | One-time summary style: **Bullets**, **Brief** (a 1 to 2 sentence TL;DR), **Detailed** (grouped by topic), or **Action items** (who's doing what, plus open questions). |
| `/mode` | Sets your default summary style. |
| `/timezone` | Sets your time zone, so clock times like `4:00 PM` mean your 4:00 PM. |
| `/forget` | Deletes everything SumUp has saved about you, after a confirmation button. |
| `/ping` | Checks that SumUp is online. |

Every reply is visible only to you ("Only you can see this"), so catching up never clutters the channel.

## How it works

1. `/sumup` reads the channel's history newest first and stops at the point you were last caught up, up to 500 messages.
2. Messages are formatted into a transcript (`[4:05 PM] maya: ...`), skipping bots and empty messages.
3. The transcript goes to GPT-6 Luna with a system prompt for the chosen style.
4. The summary comes back as a private reply, split to fit Discord's 2,000-character limit.
5. SumUp saves when you caught up, so next time it starts from there.

## Design decisions

- **Choosing the model by testing, not guessing.** GPT-6 Luna and Claude Haiku 4.5 were compared on a deliberately tricky chat log (a date change, tangents, a settled question, and a misleading price detail), three runs each. Luna was accurate in all three runs; Haiku made one factual error per run. Luna was also about 10x cheaper: roughly $0.00012 per summary.
- **Approximating "last read."** Discord doesn't share read state with bots, so SumUp infers where you left off from your last message and your last `/sumup`, ignoring your own messages from the last 10 minutes (so "what did I miss?" doesn't count). `since` gives an exact override.
- **Prompt injection defense.** Anyone in a server can write messages that the model will read. The transcript is wrapped in `<transcript>` tags and the system prompt says to treat everything inside as content, never as instructions.
- **One base prompt, four formats.** Accuracy rules (never invent details, use usernames as written) live in one shared prompt; each mode only adds its output format, so the rules can't drift between modes.
- **A custom time parser.** It accepts exactly the supported formats, rejects ambiguous numeric dates like `9/10`, and resolves missing details (no AM/PM, no year) to the most recent moment in the past.
- **Cost controls.** A 30-second per-user cooldown and 50 summaries per user per day (resetting at midnight Toronto time). Only successful summaries count. At the 500-message maximum, a summary costs about $0.0014.
- **Privacy.** SumUp stores only Discord IDs, settings, and timestamps; never message content. `/forget` deletes a user's data.

## Tech stack

- **Python 3.13**, **discord.py** (slash commands, autocomplete, buttons)
- **OpenAI API**, model GPT-6 Luna, Responses API, low reasoning effort
- **PostgreSQL** with **asyncpg** (connection pooling, parameterized queries, upserts, transactions)
- **Docker Compose** for local development
- **Railway** for hosting the bot and the production database

## Running it locally

Requirements: Python 3.13, Docker Desktop, a Discord bot token (with the Message Content intent enabled), and an OpenAI API key.

```bash
git clone https://github.com/LeeA28/sumup.git
cd sumup
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d               # starts a local Postgres
```

Create a `.env` file:

```
DISCORD_TOKEN=your_bot_token
OPENAI_API_KEY=your_openai_key
DATABASE_URL=postgresql://sumup:sumup_dev@localhost:5432/sumup
DISCORD_GUILD_ID=your_test_server_id
```

`DISCORD_GUILD_ID` is optional. When set, commands sync to that one server instantly, which is handy for development. Without it, commands sync globally.

Then run:

```bash
python bot.py
```

## Project structure

| File | Purpose |
|---|---|
| `bot.py` | Discord commands, limits, and finding which messages to summarize |
| `summarizer.py` | The OpenAI call and the prompts for each mode |
| `timeutils.py` | Parsing `since`, time zone search, and time formatting |
| `state.py` | All database access |
| `docker-compose.yml` | Local Postgres for development |
| `GUIDE.md` | A step-by-step build log with every decision and concept |
