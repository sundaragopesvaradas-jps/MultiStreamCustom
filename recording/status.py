"""UI-facing recording phase (only “recording” and “processing”)."""

from __future__ import annotations

import json
import os
from pathlib import Path

STATE_FILE = Path(
    os.environ.get(
        "ZOOM_SDK_RECORDER_STATE",
        "/opt/multistream/run/zoom-sdk-recorder.json",
    )
)
PROCESSING_FILE = Path(
    os.environ.get(
        "RECORDING_PROCESSING_FILE",
        "/opt/multistream/run/recording-processing.json",
    )
)


def set_processing(active: bool, *, detail: str = "") -> None:
    """Mark upload/finalize in progress so the UI can show “processing”."""
    if not active:
        try:
            PROCESSING_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return
    PROCESSING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROCESSING_FILE.write_text(
        json.dumps({"phase": "processing", "detail": detail}, indent=2),
        encoding="utf-8",
    )
    try:
        os.chmod(PROCESSING_FILE, 0o644)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def public_status() -> dict:
    """Return phase for the control UI.

    Only two visible phases:
      - recording  — Meeting SDK bot is capturing
      - processing — stopping / uploading to Azure Blob
    Otherwise phase is empty (UI hides the banner).
    """
    if PROCESSING_FILE.exists():
        detail = ""
        try:
            raw = json.loads(PROCESSING_FILE.read_text(encoding="utf-8"))
            detail = str(raw.get("detail") or "")
        except (OSError, json.JSONDecodeError):
            pass
        return {
            "phase": "processing",
            "label": "Recording is processing — saving to Azure Storage…",
            "detail": detail,
        }

    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        pid = state.get("pid")
        try:
            pid_i = int(pid) if pid is not None else 0
        except (TypeError, ValueError):
            pid_i = 0
        if pid_i and _pid_alive(pid_i):
            name = str(state.get("display_name") or "recorder")
            return {
                "phase": "recording",
                "label": f"Recording in progress ({name})",
                "detail": str(state.get("meeting_id") or ""),
            }

    return {"phase": "", "label": "", "detail": ""}
