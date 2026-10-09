"""Per-user data SumUp remembers, stored in Postgres.

bot.py only calls the functions below, so it doesn't need to know
any SQL. Every function is async: database calls go over the network,
and awaiting them lets the bot keep serving other users meanwhile.
"""
from datetime import date, datetime
from typing import Optional

import asyncpg

# A pool of reusable database connections, opened once at startup
_pool: Optional[asyncpg.Pool] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_settings (
    user_id   BIGINT PRIMARY KEY,   -- Discord user ID
    timezone  TEXT,                 -- like 'America/Toronto', or NULL if not set
    mode      TEXT                  -- like 'action', or NULL if not set
);

CREATE TABLE IF NOT EXISTS last_sumups (
    user_id        BIGINT NOT NULL,
    channel_id     BIGINT NOT NULL,
    last_sumup_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, channel_id)   -- one time per user per channel
);

CREATE TABLE IF NOT EXISTS daily_usage (
    user_id  BIGINT NOT NULL,
    day      DATE NOT NULL,        -- Toronto date the usage counts toward
    count    INTEGER NOT NULL,     -- successful summaries that day
    PRIMARY KEY (user_id, day)
);
"""


async def init(database_url: str) -> None:
    """Open the connection pool and make sure the tables exist."""
    global _pool
    _pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)


async def close() -> None:
    """Close every pooled connection cleanly when the bot shuts down."""
    if _pool:
        await _pool.close()


# --- Settings -----------------------------------------------------------------

async def get_timezone(user_id: int) -> Optional[str]:
    return await _pool.fetchval("SELECT timezone FROM user_settings WHERE user_id = $1", user_id)


async def set_timezone(user_id: int, zone: str) -> None:
    # Upsert: create the row if it's new, otherwise update just this column
    await _pool.execute(
        """
        INSERT INTO user_settings (user_id, timezone) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET timezone = EXCLUDED.timezone
        """,
        user_id, zone,
    )


async def get_mode(user_id: int) -> Optional[str]:
    return await _pool.fetchval("SELECT mode FROM user_settings WHERE user_id = $1", user_id)


async def set_mode(user_id: int, mode: str) -> None:
    await _pool.execute(
        """
        INSERT INTO user_settings (user_id, mode) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET mode = EXCLUDED.mode
        """,
        user_id, mode,
    )


# --- Last /sumup times --------------------------------------------------------

async def get_last_sumup(user_id: int, channel_id: int) -> Optional[datetime]:
    return await _pool.fetchval(
        "SELECT last_sumup_at FROM last_sumups WHERE user_id = $1 AND channel_id = $2",
        user_id, channel_id,
    )


async def set_last_sumup(user_id: int, channel_id: int, when: datetime) -> None:
    await _pool.execute(
        """
        INSERT INTO last_sumups (user_id, channel_id, last_sumup_at) VALUES ($1, $2, $3)
        ON CONFLICT (user_id, channel_id) DO UPDATE SET last_sumup_at = EXCLUDED.last_sumup_at
        """,
        user_id, channel_id, when,
    )


# --- Daily usage limit ----------------------------------------------------------

async def get_usage(user_id: int, day: date) -> int:
    """How many successful summaries the user has had on this day."""
    count = await _pool.fetchval(
        "SELECT count FROM daily_usage WHERE user_id = $1 AND day = $2", user_id, day
    )
    return count or 0


async def add_usage(user_id: int, day: date) -> None:
    """Count one successful summary. The +1 happens inside Postgres, so two
    commands finishing at the same moment can't overwrite each other."""
    await _pool.execute(
        """
        INSERT INTO daily_usage (user_id, day, count) VALUES ($1, $2, 1)
        ON CONFLICT (user_id, day) DO UPDATE SET count = daily_usage.count + 1
        """,
        user_id, day,
    )


async def prune_usage(before: date) -> None:
    """Delete usage rows older than `before`, so the table doesn't grow forever."""
    await _pool.execute("DELETE FROM daily_usage WHERE day < $1", before)


# --- /forget --------------------------------------------------------------------

async def forget_user(user_id: int) -> None:
    """Delete everything stored about a user except today's usage count.

    Both deletes run in one transaction: either both happen or neither does.
    """
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM user_settings WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM last_sumups WHERE user_id = $1", user_id)
