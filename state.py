"""Per-user data SumUp remembers, stored in Postgres.

bot.py only calls the functions below, so it doesn't need to know
any SQL. Every function is async: database calls go over the network,
and awaiting them lets the bot keep serving other users meanwhile.
"""
from datetime import datetime
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
