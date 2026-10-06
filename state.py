"""Per-user data SumUp remembers.

Kept in memory for now, so it resets when the bot restarts.
Step 6 replaces the inside of these functions with a database;
the rest of the code keeps calling them the same way.
"""
from datetime import datetime
from typing import Optional

_timezones: dict[int, str] = {}                        # user ID -> "America/Toronto"
_last_catchups: dict[tuple[int, int], datetime] = {}   # (user ID, channel ID) -> UTC time


def get_timezone(user_id: int) -> Optional[str]:
    return _timezones.get(user_id)


def set_timezone(user_id: int, zone: str) -> None:
    _timezones[user_id] = zone


def get_last_catchup(user_id: int, channel_id: int) -> Optional[datetime]:
    return _last_catchups.get((user_id, channel_id))


def set_last_catchup(user_id: int, channel_id: int, when: datetime) -> None:
    _last_catchups[(user_id, channel_id)] = when
