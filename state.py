"""Per-user data SumUp remembers.

Kept in memory for now, so it resets when the bot restarts.
Step 6 replaces the inside of these functions with a database;
the rest of the code keeps calling them the same way.
"""
from datetime import datetime
from typing import Optional

_timezones: dict[int, str] = {}                        # user ID -> "America/Toronto"
_modes: dict[int, str] = {}                            # user ID -> "action"
_last_sumups: dict[tuple[int, int], datetime] = {}   # (user ID, channel ID) -> UTC time


def get_timezone(user_id: int) -> Optional[str]:
    return _timezones.get(user_id)


def set_timezone(user_id: int, zone: str) -> None:
    _timezones[user_id] = zone


def get_last_sumup(user_id: int, channel_id: int) -> Optional[datetime]:
    return _last_sumups.get((user_id, channel_id))


def set_last_sumup(user_id: int, channel_id: int, when: datetime) -> None:
    _last_sumups[(user_id, channel_id)] = when


def get_mode(user_id: int) -> Optional[str]:
    return _modes.get(user_id)


def set_mode(user_id: int, mode: str) -> None:
    _modes[user_id] = mode
