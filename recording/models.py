"""Schedule and recording configuration shapes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

WEEKDAY_LABELS = {
    "monday": "Monday",
    "tuesday": "Tuesday",
    "wednesday": "Wednesday",
    "thursday": "Thursday",
    "friday": "Friday",
    "saturday": "Saturday",
    "sunday": "Sunday",
}

RETENTION_DAYS = 180  # 6 months


@dataclass
class TimeSlot:
    """One inclusive start / exclusive-end window in Asia/Kolkata local time."""

    start: str  # HH:MM
    end: str  # HH:MM

    def validate(self) -> None:
        for label, value in (("start", self.start), ("end", self.end)):
            parts = value.split(":")
            if len(parts) != 2:
                raise ValueError(f"Slot {label} must be HH:MM, got {value!r}")
            hour, minute = int(parts[0]), int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError(f"Slot {label} out of range: {value!r}")
        if self._minutes(self.end) <= self._minutes(self.start):
            raise ValueError(f"Slot end must be after start: {self.start}–{self.end}")

    @staticmethod
    def _minutes(hhmm: str) -> int:
        hour, minute = hhmm.split(":")
        return int(hour) * 60 + int(minute)

    def contains_minutes(self, minutes_since_midnight: int) -> bool:
        return self._minutes(self.start) <= minutes_since_midnight < self._minutes(self.end)


@dataclass
class RecordingConfig:
    """Owner-managed recording settings."""

    enabled: bool = False
    meeting_id: str = ""
    bot_display_name: str = "ISKCON Deoghar Archive"
    # weekday -> list of slots
    schedule: dict[str, list[TimeSlot]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "meeting_id": self.meeting_id,
            "bot_display_name": self.bot_display_name,
            "schedule": {
                day: [asdict(slot) for slot in slots]
                for day, slots in self.schedule.items()
            },
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> RecordingConfig:
        raw = raw or {}
        schedule: dict[str, list[TimeSlot]] = {}
        for day in WEEKDAYS:
            entries = raw.get("schedule", {}).get(day) or []
            slots: list[TimeSlot] = []
            for entry in entries:
                if isinstance(entry, str):
                    # Allow "05:00-18:00" shorthand from the form.
                    if "-" not in entry:
                        continue
                    start, end = entry.split("-", 1)
                    slot = TimeSlot(start=start.strip(), end=end.strip())
                else:
                    slot = TimeSlot(
                        start=str(entry.get("start", "")).strip(),
                        end=str(entry.get("end", "")).strip(),
                    )
                if slot.start and slot.end:
                    slot.validate()
                    slots.append(slot)
            schedule[day] = slots
        return cls(
            enabled=bool(raw.get("enabled")),
            meeting_id=str(raw.get("meeting_id") or "").strip(),
            bot_display_name=(
                str(raw.get("bot_display_name") or "ISKCON Deoghar Archive").strip()
                or "ISKCON Deoghar Archive"
            ),
            schedule=schedule,
        )


@dataclass
class ActiveWindow:
    weekday: str
    slot: TimeSlot
    label: str  # e.g. monday 05:00-18:00
