# Σ SumUp

A Discord bot that tells you what you missed. Run `/sumup` in any channel and get a private, AI-written summary of everything since you were last there: decisions, who's doing what, and questions still open.

Built with Python, discord.py, the OpenAI API (GPT-6 Luna), and PostgreSQL. Deployed on Railway.

**[Add SumUp to your server](https://discord.com/oauth2/authorize?client_id=1411077447750782986&permissions=84992&integration_type=0&scope=applications.commands+bot)**

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

### The big picture

```mermaid
flowchart LR
    U["You, in Discord<br/>slash commands only"] <--> B["SumUp bot<br/>Python + discord.py<br/>on Railway"]
    B <--> D[("Postgres on Railway<br/>settings, last-SumUp times,<br/>daily counts")]
    B <--> O["OpenAI GPT-6 Luna<br/>writes the summary"]
```

Every reply is ephemeral: only the person who ran the command sees it.

### What happens when you run `/sumup`

```mermaid
flowchart TD
    A["/sumup<br/>optional: since, mode"] --> B["Private placeholder:<br/>SumUp is thinking..."]
    B --> C{"Ran /sumup in the<br/>last 30 seconds?"}
    C -- Yes --> C1["Slow down a little!<br/>Try again in N seconds."]
    C -- No --> D{"50 SumUps<br/>already today?"}
    D -- Yes --> D1["You've used all 50 SumUps for today.<br/>Your limit resets in X hours."]
    D -- No --> E{"since given?"}
    E -- Yes --> F{"Can SumUp read it?"}
    F -- No --> F1["Explains the problem,<br/>for example: That time is in the future."]
    F -- Yes --> G["Read messages after that time<br/>up to 500"]
    E -- No --> H["Read newest to oldest until your last /sumup here<br/>or your own message older than 10 minutes<br/>up to 500"]
    G --> I{"Any new messages<br/>from people?"}
    H --> I
    I -- No --> I1["You're all caught up!<br/>No new messages."]
    I -- Yes --> J["Build transcript<br/>[4:05 PM] maya: ...<br/>skip bots, keep attachments"]
    J --> K["Pick style: the mode option,<br/>else your /mode default,<br/>else Bullets"]
    K --> L["GPT-6 Luna writes the summary"]
    L -- Error --> L1["Sorry, I couldn't reach the summarizer<br/>right now. Try again in a moment."]
    L -- OK --> M["Private reply:<br/>Σ SumUp (Bullets): 20 messages since ..."]
    M --> N["Save this as your last /sumup here<br/>and add 1 to today's count"]
```

Step by step, with what's happening behind the scenes:

1. **You type `/sumup`.** While you type `since`, a live preview under the box shows how SumUp reads it, like `→ Yesterday at 4:00 PM (Toronto)`. `/sumup` is hidden in DMs because it needs a channel.
2. **"SumUp is thinking…"** Discord requires a reply within 3 seconds, so the bot sends a private placeholder right away and then has 15 minutes for the real answer.
3. **Limit check.** A 30-second cooldown (kept in the bot's memory) and 50 summaries per day (stored in Postgres, resetting at midnight Toronto time). The cooldown starts immediately, so double-clicks can't run two summaries.
4. **Finding where you left off.** Discord doesn't share "last read" with bots, so without `since` SumUp reads newest to oldest and stops at your last `/sumup` in that channel or your last message there, whichever comes first. Your own messages from the last 10 minutes don't count, so typing "what did I miss?" first still works. No starting point within 500 messages means it uses the last 500.
5. **Building the transcript.** Each message becomes `[time] name: text`. Bots (including SumUp) and empty messages are skipped, files show as `[attachment: photo.png]`, and mentions show as names. Times use your saved time zone, or UTC (labeled "UTC") if you haven't set one.
6. **Picking the style.** The `mode` option wins, then your `/mode` default, then Bullets. Summaries of more than 200 messages may use up to 8 bullets instead of 6.
7. **Summarizing.** The transcript is wrapped in `<transcript>` tags and sent to GPT-6 Luna with the mode's instructions. Anything inside the tags is treated as chat, never as instructions.
8. **Your summary.** The header's time is a Discord timestamp, so everyone sees it in their own time zone. Replies over 2,000 characters are split at line breaks. SumUp then saves this moment as your last `/sumup` in that channel and adds 1 to your daily count.

If a run ends without a summary (a bad `since`, nothing new, a permission problem, or an OpenAI error), it doesn't use up your cooldown or count toward the daily limit.

### The note under the summary header

| Situation | Note |
|---|---|
| Normal catch-up | No note; the header's "since" time says it all |
| First SumUp in this channel, over 500 messages | This is your first SumUp here, so this covers the last 500 messages. Next time it'll pick up where you left off. |
| First SumUp in this channel, small channel | This is your first SumUp here, so this covers the whole channel. Next time it'll pick up where you left off. |
| Away a long time, or `since` reaches back past 500 messages | You missed more than 500 messages, so this covers the most recent 500. |

### What you can type for `since`

Missing details resolve to the most recent moment that has already happened. For example, `4:00` could mean 4 AM or 4 PM, today or yesterday, so SumUp picks the latest one in the past.

| You type | Accepted? | How it's read |
|---|---|---|
| `2h`, `30m`, `1d 3h`, `2 hours ago` | ✅ | Counted back from now. Works without a time zone. |
| `4:00 PM`, `4pm`, `16:00` | ✅ | The most recent 4:00 PM. Needs `/timezone`. |
| `4:00` (no AM/PM) | ✅ | The most recent 4:00, AM or PM. |
| `yesterday 9pm`, `today 9am` | ✅ | That day at that time. |
| `Sep 22 6:23 PM`, `6:23 PM on September 22, 2026` | ✅ | Matches Discord's "new messages since" banner. No year means the most recent Sep 22. |
| `Sep 22` | ✅ | The start of that day. |
| `9/22` | ❌ | "Write dates with the month name, like Sep 22 6:23 PM." (`9/10` could mean Sept 10 or Oct 9) |
| `today 11pm` when it's 9 PM | ❌ | "That time is in the future." |
| `Feb 30` | ❌ | "That date doesn't exist." |
| `13pm`, `25:00`, `4:75` | ❌ | "With AM/PM, the hour must be 1 to 12." / "Hours go up to 23." / "Minutes go up to 59." |
| `yesterday Sep 22` | ❌ | "Use either a date or today/yesterday, not both." |
| A clock time before setting a time zone | ❌ | "Set your time zone with /timezone first, or use a relative time like 2h." |

### Summary modes

| Mode | What you get |
|---|---|
| **Bullets** (default) | 3 to 6 points (up to 8 for big catch-ups): decisions and who agreed, who's doing what, and open questions |
| **Brief** | A 1 to 2 sentence TL;DR, no bullets |
| **Detailed** | Grouped by topic, with who said what, plus a "Still open" section |
| **Action items** | Only tasks as "name: task", then open questions, or "No action items." |

### The other commands

- **`/mode`**: pick Bullets, Brief, Detailed, or Action items from a dropdown to save it as your default. Run it with nothing picked to see your current default. Applies in every server.
- **`/timezone`**: start typing a city; suggestions show each zone's current local time, like `Toronto (America/Toronto), now 4:12 PM`. Daylight saving is handled automatically. Anything not in the list gets "I don't recognize that time zone. Start typing a city and pick from the list."
- **`/forget`**: explains what will be deleted, with **Delete my data** and **Cancel** buttons. Only the person who ran it can press them, and they expire after 60 seconds. Deleting removes your time zone, default mode, and every last-SumUp time in one transaction. Today's usage count is kept so `/forget` can't be used to get around the daily limit.
- **`/ping`**: replies "Σ Pong! Latency: 42 ms" (the delay between the bot and Discord).

### What SumUp stores

| Table | Contents | Kept |
|---|---|---|
| `user_settings` | Discord user ID, time zone, default mode | Until `/forget` |
| `last_sumups` | User ID, channel ID, when you last ran `/sumup` there | Until `/forget` |
| `daily_usage` | User ID, date, number of summaries | 3 days |

Message text and summaries are never stored. Each summary does send that conversation's transcript to OpenAI so the model can write it.

### Errors you might see

| Cause | Message | Uses a SumUp? |
|---|---|---|
| SumUp can't read that channel's history | I don't have permission to read this channel's history. | No |
| OpenAI is down or returns an error | Sorry, I couldn't reach the summarizer right now. Try again in a moment. | No |
| OpenAI returns an empty summary | The summarizer returned nothing. Try again. | No |
| Anything unexpected, like the database being unreachable | Something went wrong on my end. Please try again in a moment. | No |

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
