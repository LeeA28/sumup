# Σ SumUp

A Discord bot that tells you what you missed. Run `/sumup` in any channel and get a private, AI-written summary of everything since you were last there: decisions, who's doing what, and questions still open.

Built with Python, discord.py, the OpenAI API (GPT-6 Luna), and PostgreSQL. Deployed on Railway.

**[Add SumUp to your server](https://discord.com/oauth2/authorize?client_id=1411077447750782986&permissions=84992&integration_type=0&scope=applications.commands+bot)**

## Commands

| Command | What it does |
|---|---|
| `/sumup` | Summarizes what you missed in this channel. Picks up from your last `/sumup` or your last message, whichever is more recent. New here? You get the last 200 messages. |
| `/sumup since:2h` | Summarizes from a time you choose: `2h`, `30m`, `4:00 PM`, `yesterday 9pm`, or `Sep 22 6:23 PM` (the format of Discord's "new messages since" banner). A live preview shows how SumUp reads your input as you type. |
| `/sumup mode:` | One-time summary style: **Bullets** (quick points) or **Detailed** (grouped by topic, with to-dos and open questions). |
| `/mode` | Sets your default summary style. |
| `/timezone` | Sets your time zone, so clock times like `4:00 PM` mean your 4:00 PM. |
| `/forget` | Deletes everything SumUp has saved about you, after a confirmation button. |
| `/help` | Shows what SumUp does and how to use it. |

Every reply is visible only to you ("Only you can see this"), so catching up never clutters the channel. Under each summary, **Share to channel** posts it publicly and **Switch to Detailed / Bullets** rewrites it in the other style.

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
    F -- Yes --> G["Read messages after that time<br/>up to 1,000"]
    E -- No --> H["Read newest to oldest until your last /sumup here<br/>or your own message older than 10 minutes<br/>up to 1,000"]
    H --> H2{"First SumUp here and<br/>no message of yours found?"}
    H2 -- Yes --> H3["Keep only the<br/>last 200 messages"]
    H2 -- No --> I
    H3 --> I
    G --> I{"Any new messages<br/>from people?"}
    I -- No --> I1["You're all caught up!<br/>with a Summarize again button"]
    I -- Yes --> J["Build transcript<br/>[4:05 PM] maya: ...<br/>skip bots, keep attachments"]
    J --> K["Pick style: the mode option,<br/>else your /mode default,<br/>else Bullets"]
    K --> L0{"More than 250<br/>messages?"}
    L0 -- No --> L["GPT-6 Luna writes the summary<br/>in one call"]
    L0 -- Yes --> LC["Split into chunks of up to 250<br/>and take notes on each at the same time"]
    LC --> LR["GPT-6 Luna combines the notes<br/>into one summary"]
    LR -- Error --> L1
    LR -- OK --> N
    L -- Error --> L1["Sorry, I couldn't reach the summarizer<br/>right now. Try again in a moment."]
    L -- OK --> N["Save this as your last /sumup here<br/>and add 1 to today's count"]
    N --> M["Private reply:<br/>Σ SumUp (Bullets): 20 messages since ...<br/>with Share to channel and Switch buttons"]
```

Step by step, with what's happening behind the scenes:

1. **You type `/sumup`.** While you type `since`, a live preview under the box shows how SumUp reads it, like `→ Yesterday at 4:00 PM (Toronto)`. `/sumup` is hidden in DMs because it needs a channel.
2. **"SumUp is thinking…"** Discord requires a reply within 3 seconds, so the bot sends a private placeholder right away and then has 15 minutes for the real answer.
3. **Limit check.** A 30-second cooldown (kept in the bot's memory) and 50 summaries per day (stored in Postgres, resetting at midnight Toronto time). The cooldown starts immediately, so double-clicks can't run two summaries.
4. **Finding where you left off.** Discord doesn't share "last read" with bots, so without `since` SumUp reads newest to oldest and stops at your last `/sumup` in that channel or your last message there, whichever comes first. Your own messages from the last 10 minutes don't count, so typing "what did I miss?" first still works. It reads up to 1,000 messages. If this is your first SumUp in the channel and none of your messages turn up, it keeps just the last 200 as background.
5. **Building the transcript.** Each message becomes `[time] name: text`. Bots (including SumUp) and empty messages are skipped, files show as `[attachment: photo.png]`, and mentions show as names. Times use your saved time zone, or UTC (labeled "UTC") if you haven't set one.
6. **Picking the style.** The `mode` option wins, then your `/mode` default, then Bullets. Summaries of more than 200 messages may use up to 8 bullets instead of 6.
7. **Summarizing.** The transcript is wrapped in `<transcript>` tags and sent to GPT-6 Luna with the mode's instructions. Anything inside the tags is treated as chat, never as instructions. The model is also told the transcript may start mid-conversation, so it says when earlier context is missing instead of guessing.
   - **Long catch-ups (over 250 messages) use map-reduce summarization:** the messages are split into chunks of up to 250, the model takes detailed notes on every chunk at the same time, and one final call combines the notes into the summary, keeping the latest version of any plan that changed. 1,000 messages means 4 chunks plus 1 combine call. It still counts as one SumUp.
8. **Your summary.** The header's time is a Discord timestamp, so everyone sees it in their own time zone. Replies over 2,000 characters are split at line breaks. SumUp saves this moment as your last `/sumup` in that channel and adds 1 to your daily count.
9. **The buttons** (they work for about 14 minutes, until just before Discord stops letting the bot edit private replies):
   - **Share to channel** posts the summary publicly as "Σ SumUp (Bullets), shared by Andy". Mentions are turned off, so nobody gets pinged. Each summary can be shared once, and sharing doesn't use a SumUp.
   - **Switch to Detailed / Switch to Bullets** rewrites the same messages in the other style as a new private message. Each style is written at most once per summary, so switching back is instant and free. The first switch uses one SumUp but skips the 30-second cooldown.

10. **Summarize again.** Private replies vanish if you dismiss them or reload Discord. If you run `/sumup` with nothing new, the "all caught up" reply has a **Summarize again** button that brings back your last summary for that channel:
    - **Within an hour:** SumUp shows the exact same summary from a short-term copy kept in memory. Instant and free.
    - **After an hour, or after the bot restarts:** it re-reads the same messages (it saved where your last summary started) and writes a fresh one, which uses one SumUp.

If a run ends without a summary (a bad `since`, nothing new, a permission problem, or an OpenAI error), it doesn't use up your cooldown or count toward the daily limit.

### The note under the summary header

| Situation | Note |
|---|---|
| Normal catch-up | No note; the header's "since" time says it all |
| First SumUp in this channel, over 200 messages | This is your first SumUp here, so this covers the last 200 messages. Next time it'll pick up where you left off. New to SumUp? Try `/help`. |
| First SumUp in this channel, small channel | This is your first SumUp here, so this covers the whole channel. Next time it'll pick up where you left off. New to SumUp? Try `/help`. |
| Away a long time, or `since` reaches back past 1,000 messages | You missed more than 1,000 messages, so this covers the most recent 1,000. |

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
| **Detailed** | Grouped by topic with who said what, then a "To-dos" section ("name: task") and a "Still open" section |

Earlier versions also had Brief and Action items. They were folded into these two: Bullets is already short, and Detailed's To-dos section covers action items. Anyone who had one of them saved as their default gets Bullets.

### The other commands

- **`/help`**: a private overview of the commands, the `since` formats, the two styles, the buttons, and the limits.
- **`/mode`**: pick Bullets or Detailed from a dropdown to save it as your default. Run it with nothing picked to see your current default. Applies in every server.
- **`/timezone`**: start typing a city; suggestions show each zone's current local time, like `Toronto (America/Toronto), now 4:12 PM`. Daylight saving is handled automatically. Anything not in the list gets "I don't recognize that time zone. Start typing a city and pick from the list."
- **`/forget`**: explains what will be deleted, with **Delete my data** and **Cancel** buttons. Only the person who ran it can press them, and they expire after 60 seconds. Deleting removes your time zone, default mode, and every last-SumUp time in one transaction. Today's usage count is kept so `/forget` can't be used to get around the daily limit.

### What SumUp stores

| Table | Contents | Kept |
|---|---|---|
| `user_settings` | Discord user ID, time zone, default mode | Until `/forget` |
| `last_sumups` | User ID, channel ID, when you last ran `/sumup` there, and where that summary started | Until `/forget` |
| `daily_usage` | User ID, date, number of summaries | 3 days |

Message text and summaries are never saved to the database. Your most recent summary in each channel is kept **in the bot's memory for up to an hour** so Summarize again can show it instantly; it's gone sooner if the bot restarts, and `/forget` clears it. Each summary does send that conversation's transcript to OpenAI so the model can write it.

### Errors you might see

| Cause | Message | Uses a SumUp? |
|---|---|---|
| SumUp can't read that channel's history | I don't have permission to read this channel's history. | No |
| Share pressed in a channel where SumUp can't post | I don't have permission to post in this channel. | No |
| OpenAI is down or returns an error | Sorry, I couldn't reach the summarizer right now. Try again in a moment. | No |
| OpenAI returns an empty summary | The summarizer returned nothing. Try again. | No |
| Anything unexpected, like the database being unreachable | Something went wrong on my end. Please try again in a moment. | No |

## Design decisions

- **Choosing the model by testing, not guessing.** GPT-6 Luna and Claude Haiku 4.5 were compared on a deliberately tricky chat log (a date change, tangents, a settled question, and a misleading price detail), three runs each. Luna was accurate in all three runs; Haiku made one factual error per run. Luna was also about 10x cheaper: roughly $0.00012 per summary.
- **Approximating "last read."** Discord doesn't share read state with bots, so SumUp infers where you left off from your last message and your last `/sumup`, ignoring your own messages from the last 10 minutes (so "what did I miss?" doesn't count). `since` gives an exact override.
- **Prompt injection defense.** Anyone in a server can write messages that the model will read. The transcript is wrapped in `<transcript>` tags and the system prompt says to treat everything inside as content, never as instructions.
- **One base prompt, four formats.** Accuracy rules (never invent details, use usernames as written) live in one shared prompt; each mode only adds its output format, so the rules can't drift between modes.
- **A custom time parser.** It accepts exactly the supported formats, rejects ambiguous numeric dates like `9/10`, and resolves missing details (no AM/PM, no year) to the most recent moment in the past.
- **Map-reduce for long conversations.** Catch-ups over 250 messages are split into chunks summarized in parallel, then combined. Each chunk stays small enough for the model to read closely, and running them at the same time keeps it about as fast as two calls. The chunk size is one setting (`CHUNK_SIZE`) so it can be tuned after testing.
- **Fewer, clearer styles.** Four modes became two after noticing they overlapped: Bullets and Detailed form a simple short/long choice, and Detailed absorbed action items as a To-dos section.
- **Cost controls.** A 30-second per-user cooldown and 50 summaries per user per day (resetting at midnight Toronto time). Only successful summaries count, and switching styles reuses a style that was already written. At the 1,000-message maximum, a summary costs about $0.003.
- **A short-term cache instead of saved summaries.** Re-reading a dismissed summary uses an in-memory cache with a one-hour expiry rather than the database, because summaries contain conversation content. A cache hit is free and shows the identical summary; a miss falls back to rewriting it from the saved range. The limit: the cache lives in one bot process, so running several copies of SumUp would need a shared cache such as Redis.
- **Privacy.** The database holds only Discord IDs, settings, and timestamps, never message content. `/forget` deletes a user's data, including anything in the cache.

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
| `bot.py` | Discord commands, buttons, limits, and finding which messages to summarize |
| `summarizer.py` | The OpenAI calls, the prompts for each style, and chunking for long conversations |
| `timeutils.py` | Parsing `since`, time zone search, and time formatting |
| `state.py` | All database access |
| `docker-compose.yml` | Local Postgres for development |
| `GUIDE.md` | A step-by-step build log with every decision and concept |
