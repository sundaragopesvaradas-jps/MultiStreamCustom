"""IST clock helpers for schedule matching."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .models import ActiveWindow, RecordingConfig, WEEKDAYS

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(tz=IST)


def active_windows(config: RecordingConfig, when: datetime | None = None) -> list[ActiveWindow]:
    """Return schedule windows that contain `when` (default: now in IST)."""
    when = when or now_ist()
    weekday = WEEKDAYS[when.weekday()]
    minutes = when.hour * 60 + when.minute
    found: list[ActiveWindow] = []
    for slot in config.schedule.get(weekday) or []:
        if slot.contains_minutes(minutes):
            found.append(
                ActiveWindow(
                    weekday=weekday,
                    slot=slot,
                    label=f"{weekday} {slot.start}-{slot.end}",
                )
            )
    return found


def in_scheduled_window(config: RecordingConfig, when: datetime | None = None) -> ActiveWindow | None:
    windows = active_windows(config, when)
    return windows[0] if windows else None
