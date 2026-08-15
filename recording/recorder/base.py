"""Recorder interface — swap Zoom SDK / future backends without touching the pipeline."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RecorderSession:
    """A recording attempt that may still be running or already finished."""

    meeting_id: str
    output_path: Path
    display_name: str
    pid: int | None = None
    started_at: str = ""


@dataclass
class RecorderResult:
    ok: bool
    output_path: Path | None
    message: str


class MeetingRecorder(ABC):
    """Join a Zoom meeting and capture media to a local file."""

    @abstractmethod
    def is_running(self) -> bool:
        ...

    @abstractmethod
    def start(
        self,
        *,
        meeting_id: str,
        display_name: str,
        output_dir: Path,
    ) -> RecorderSession:
        ...

    @abstractmethod
    def stop(self) -> RecorderResult:
        """Stop capture and return the finished file (if any)."""

    @abstractmethod
    def status(self) -> dict:
        ...
