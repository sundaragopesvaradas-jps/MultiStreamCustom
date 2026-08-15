"""Ask Zoom whether a meeting is currently in progress."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

_UI = Path(__file__).resolve().parent.parent / "ui"
if _UI.is_dir() and str(_UI) not in sys.path:
    sys.path.insert(0, str(_UI))

import zoom  # noqa: E402

GetSecret = Callable[[str], str]


class MeetingStatusError(Exception):
    pass


def is_in_progress(get_secret: GetSecret, meeting_id: str) -> bool:
    try:
        return zoom.meeting_started(get_secret, meeting_id)
    except zoom.ZoomError as exc:
        raise MeetingStatusError(str(exc)) from exc


def join_details(get_secret: GetSecret, meeting_id: str) -> dict:
    """Passcode + local-recording token the SDK bot needs to join and capture.

    A missing recording token is reported rather than raised: the bot can still
    join and the host can grant recording manually.
    """
    details = {"passcode": "", "recording_token": "", "warning": ""}
    try:
        details["passcode"] = zoom.meeting_passcode(get_secret, meeting_id)
    except zoom.ZoomError as exc:
        raise MeetingStatusError(str(exc)) from exc
    try:
        details["recording_token"] = zoom.local_recording_token(get_secret, meeting_id)
    except zoom.ZoomError as exc:
        details["warning"] = str(exc)
    return details
