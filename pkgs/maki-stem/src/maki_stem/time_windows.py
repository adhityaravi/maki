"""Time-window predicates shared by loops and the interactive turn path.

Both ``quiet_hours`` and ``work_hours`` are read from cortex KV config and
default to sensible night-time / early-morning windows. Pure functions —
no globals, no I/O — so they're safe to import anywhere.
"""

from __future__ import annotations

from datetime import datetime


def _window_contains(now: datetime, start_str: str, end_str: str) -> bool:
    """Return True if ``now`` (local time) is inside the [start, end) window.

    Windows that cross midnight (start > end) are handled by wrapping.
    Both ``start_str`` and ``end_str`` are ``HH:MM`` strings.
    """
    current = now.hour * 60 + now.minute

    start_parts = start_str.split(":")
    end_parts = end_str.split(":")
    start = int(start_parts[0]) * 60 + int(start_parts[1])
    end = int(end_parts[0]) * 60 + int(end_parts[1])

    if start > end:  # spans midnight (e.g., 23:00 - 07:00)
        return current >= start or current < end
    return start <= current < end


def in_quiet_hours(config: dict) -> bool:
    """Return True if the current local time is within quiet hours.

    Defaults: 23:00 - 07:00. Overridable via ``quiet_hours_start`` /
    ``quiet_hours_end`` in the cortex config KV.
    """
    return _window_contains(
        datetime.now(),
        config.get("quiet_hours_start", "23:00"),
        config.get("quiet_hours_end", "07:00"),
    )


def in_work_hours(config: dict) -> bool:
    """Return True if the current local time is within work hours.

    Defaults: 01:00 - 06:00. Overridable via ``work_hours_start`` /
    ``work_hours_end`` in the cortex config KV.
    """
    return _window_contains(
        datetime.now(),
        config.get("work_hours_start", "01:00"),
        config.get("work_hours_end", "06:00"),
    )
