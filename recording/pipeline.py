"""One scheduler tick: join/leave, upload, email, retention."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import blobstore, clock, meeting, store
from .models import RETENTION_DAYS, RecordingConfig
from .recorder import RecorderNotInstalled, ZoomSdkRecorder

log = logging.getLogger("multistream.recording")

OUTPUT_DIR = Path(
    os.environ.get("RECORDING_OUTPUT_DIR", "/var/lib/multistream/recordings")
)
ALERT_STATE = Path(
    os.environ.get(
        "RECORDING_ALERT_STATE",
        "/opt/multistream/run/recording-alert-state.json",
    )
)

GetSecret = Callable[[str], str]
GetOptional = Callable[[str], str | None]
SendAlert = Callable[..., bool]


def _ui_path() -> Path:
    return Path(__file__).resolve().parent.parent / "ui"


def _secret_helpers() -> tuple[GetSecret, GetOptional, SendAlert]:
    ui = _ui_path()
    if str(ui) not in sys.path:
        sys.path.insert(0, str(ui))
    import keyvault  # noqa: WPS433
    import notify  # noqa: WPS433

    vault = os.environ.get("KEY_VAULT_NAME") or ""
    if not vault:
        cfg = Path("/opt/multistream/etc/multistream.env")
        if cfg.exists():
            for line in cfg.read_text().splitlines():
                if line.startswith("KEY_VAULT_NAME="):
                    vault = line.split("=", 1)[1].strip().strip("'\"")
                    break

    def get_secret(name: str) -> str:
        return keyvault.get_secret(vault, name)

    def get_optional(name: str) -> str | None:
        try:
            value = get_secret(name).strip()
        except Exception:  # noqa: BLE001
            return None
        return value or None

    def send_alert(*, subject: str, body: str) -> bool:
        return notify.send_alert(get_optional, subject=subject, body=body)

    return get_secret, get_optional, send_alert


def _load_alert_state() -> dict:
    import json

    if not ALERT_STATE.exists():
        return {}
    try:
        return json.loads(ALERT_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_alert_state(state: dict) -> None:
    import json

    ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
    ALERT_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def tick(config: RecordingConfig | None = None) -> dict:
    """Run one scheduler iteration. Safe to call every minute."""
    get_secret, get_optional, send_alert = _secret_helpers()
    config = config or store.load()
    recorder = ZoomSdkRecorder(get_secret, get_optional)
    result: dict = {
        "enabled": config.enabled,
        "action": "idle",
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    if not config.enabled:
        if recorder.is_running():
            result["action"] = "stop_disabled"
            _finalize_recording(recorder, get_optional, send_alert, reason="recording disabled")
        return result

    if not config.meeting_id:
        result["action"] = "no_meeting_id"
        return result

    window = clock.in_scheduled_window(config)
    if window is None:
        if recorder.is_running():
            result["action"] = "stop_outside_window"
            _finalize_recording(
                recorder, get_optional, send_alert, reason="schedule window ended"
            )
        return result

    result["window"] = window.label

    # Inside a window — meeting must be live.
    try:
        live = meeting.is_in_progress(get_secret, config.meeting_id)
    except Exception as exc:  # noqa: BLE001
        log.error("Meeting status check failed: %s", exc)
        result["action"] = "status_error"
        result["error"] = str(exc)
        return result

    if not live:
        result["action"] = "meeting_not_running"
        _alert_meeting_missing(send_alert, config, window.label)
        if recorder.is_running():
            _finalize_recording(
                recorder, get_optional, send_alert, reason="meeting ended during window"
            )
        return result

    # Clear the "missing meeting" alert latch once it is back.
    state = _load_alert_state()
    if state.get("missing_window") == window.label:
        state.pop("missing_window", None)
        _save_alert_state(state)

    if recorder.is_running():
        result["action"] = "already_recording"
        result["recorder"] = recorder.status()
        return result

    try:
        session = recorder.start(
            meeting_id=config.meeting_id,
            display_name=config.bot_display_name,
            output_dir=OUTPUT_DIR,
        )
    except RecorderNotInstalled as exc:
        result["action"] = "sdk_not_installed"
        result["error"] = str(exc)
        send_alert(
            subject="ISKCON recording: Meeting SDK recorder not installed",
            body=str(exc),
        )
        return result
    except Exception as exc:  # noqa: BLE001
        result["action"] = "start_failed"
        result["error"] = str(exc)
        send_alert(
            subject="ISKCON recording: failed to start recorder",
            body=f"Window {window.label}\nMeeting {config.meeting_id}\n\n{exc}",
        )
        return result

    result["action"] = "started"
    result["session"] = {
        "pid": session.pid,
        "output": str(session.output_path),
        "display_name": session.display_name,
    }
    return result


def _alert_meeting_missing(send_alert: SendAlert, config: RecordingConfig, window: str) -> None:
    state = _load_alert_state()
    # One email per window label per day-ish — latch until meeting appears or window changes.
    if state.get("missing_window") == window:
        return
    send_alert(
        subject="ISKCON recording: meeting not running in scheduled slot",
        body=(
            f"Schedule window: {window} (IST)\n"
            f"Meeting ID: {config.meeting_id}\n"
            f"Bot name: {config.bot_display_name}\n\n"
            "The recorder skipped this slot because Zoom reports the meeting "
            "is not in progress. Start the meeting (or ignore if it was intentional)."
        ),
    )
    state["missing_window"] = window
    _save_alert_state(state)


def _finalize_recording(
    recorder: ZoomSdkRecorder,
    get_optional: GetOptional,
    send_alert: SendAlert,
    *,
    reason: str,
) -> None:
    from . import status as recording_status

    recording_status.set_processing(True, detail=reason)
    try:
        stopped = recorder.stop()
        if not stopped.ok or not stopped.output_path:
            send_alert(
                subject="ISKCON recording: stop with no file",
                body=f"Reason: {reason}\n{stopped.message}",
            )
            return

        local = stopped.output_path
        blob_name = local.name
        try:
            url = blobstore.upload_file(get_optional, local, blob_name=blob_name)
        except Exception as exc:  # noqa: BLE001
            send_alert(
                subject="ISKCON recording: upload failed",
                body=f"Reason: {reason}\nLocal file: {local}\n\n{exc}",
            )
            return

        send_alert(
            subject="ISKCON recording: saved to Azure Storage",
            body=(
                f"Reason: {reason}\n"
                f"Local file: {local}\n"
                f"Blob: {blob_name}\n"
                f"URL: {url}\n"
                f"Retention: {RETENTION_DAYS} days\n"
            ),
        )
        try:
            local.unlink(missing_ok=True)
        except OSError:
            pass
    finally:
        recording_status.set_processing(False)

def purge_retention() -> list[str]:
    _, get_optional, send_alert = _secret_helpers()
    try:
        deleted = blobstore.purge_older_than(get_optional, days=RETENTION_DAYS)
    except Exception as exc:  # noqa: BLE001
        send_alert(
            subject="ISKCON recording: retention purge failed",
            body=str(exc),
        )
        raise
    if deleted:
        send_alert(
            subject="ISKCON recording: retention purge",
            body=f"Deleted {len(deleted)} blob(s) older than {RETENTION_DAYS} days:\n"
            + "\n".join(deleted[:50]),
        )
    return deleted
