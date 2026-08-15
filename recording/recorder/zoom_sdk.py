"""Zoom Meeting SDK raw-data recorder adapter.

The proprietary Linux Meeting SDK binary is installed separately at
`/opt/multistream/bin/zoom-sdk-recorder` (see docs/RECORDING_SETUP.md).

This module:
  1. Builds the Meeting SDK JWT from zoom-sdk-key / zoom-sdk-secret
  2. Writes a job manifest the binary understands
  3. Starts/stops that process and reports the output file

Until the binary is installed, start() raises RecorderNotInstalled so the
pipeline can email a clear alert instead of failing silently.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .base import MeetingRecorder, RecorderResult, RecorderSession

try:
    import jwt  # PyJWT
except ImportError:  # pragma: no cover
    jwt = None  # type: ignore

GetSecret = Callable[[str], str]
GetOptional = Callable[[str], str | None]

DEFAULT_BIN = Path(
    os.environ.get("ZOOM_SDK_RECORDER_BIN", "/opt/multistream/bin/zoom-sdk-recorder")
)
STATE_FILE = Path(
    os.environ.get(
        "ZOOM_SDK_RECORDER_STATE",
        "/opt/multistream/run/zoom-sdk-recorder.json",
    )
)
JOB_DIR = Path(os.environ.get("ZOOM_SDK_RECORDER_JOB_DIR", "/opt/multistream/run/recording-jobs"))

# After the bot leaves, the wrapper still has to drain the encoder's buffers and
# rewrite the MP4 index. Killing it early leaves an unplayable file, so wait
# generously — the UI shows "Processing" for the whole window.
STOP_TIMEOUT_SECONDS = int(os.environ.get("ZOOM_SDK_RECORDER_STOP_TIMEOUT", "660"))


class RecorderError(Exception):
    pass


class RecorderNotInstalled(RecorderError):
    pass


def _sdk_jwt(sdk_key: str, sdk_secret: str, meeting_number: str, *, role: int = 0) -> str:
    if jwt is None:
        raise RecorderError("PyJWT is not installed in the UI venv (pip install PyJWT).")
    now = int(time.time())
    payload = {
        "appKey": sdk_key,
        "sdkKey": sdk_key,
        "mn": meeting_number,
        "role": role,
        "iat": now,
        "exp": now + 60 * 60 * 2,
        "tokenExp": now + 60 * 60 * 2,
    }
    return jwt.encode(payload, sdk_secret, algorithm="HS256")


class ZoomSdkRecorder(MeetingRecorder):
    def __init__(
        self,
        get_secret: GetSecret,
        get_optional: GetOptional,
        *,
        binary: Path | None = None,
    ) -> None:
        self._get_secret = get_secret
        self._get_optional = get_optional
        self._binary = binary or DEFAULT_BIN

    def is_running(self) -> bool:
        state = self._read_state()
        pid = state.get("pid")
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
        except OSError:
            return False
        return True

    def start(
        self,
        *,
        meeting_id: str,
        display_name: str,
        output_dir: Path,
        passcode: str = "",
        recording_token: str = "",
    ) -> RecorderSession:
        if self.is_running():
            state = self._read_state()
            return RecorderSession(
                meeting_id=str(state.get("meeting_id") or meeting_id),
                output_path=Path(state.get("output_path") or output_dir),
                display_name=str(state.get("display_name") or display_name),
                pid=int(state["pid"]) if state.get("pid") else None,
                started_at=str(state.get("started_at") or ""),
            )

        if not self._binary.exists():
            raise RecorderNotInstalled(
                f"Meeting SDK recorder binary not found at {self._binary}. "
                "Follow docs/RECORDING_SETUP.md to install the Zoom Linux Meeting SDK raw-data sample."
            )
        try:
            head = self._binary.read_text(encoding="utf-8", errors="ignore")[:400]
        except OSError:
            head = ""
        if "not installed yet" in head:
            raise RecorderNotInstalled(
                f"Meeting SDK recorder at {self._binary} is still the placeholder. "
                "Run /opt/multistream/bin/zoom-sdk-build.sh on the VM after placing the SDK tarball."
            )

        sdk_key = self._get_optional("zoom-sdk-key")
        sdk_secret = self._get_optional("zoom-sdk-secret")
        if not sdk_key or not sdk_secret:
            raise RecorderError(
                "zoom-sdk-key / zoom-sdk-secret missing in Key Vault. "
                "Create a Meeting SDK app with raw data enabled."
            )

        digits = "".join(ch for ch in meeting_id if ch.isdigit())
        token = _sdk_jwt(sdk_key, sdk_secret, digits, role=0)
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_path = output_dir / f"zoom-{digits}-{stamp}.mp4"

        JOB_DIR.mkdir(parents=True, exist_ok=True)
        job_path = JOB_DIR / f"job-{stamp}.json"
        job = {
            "meeting_number": digits,
            "token": token,
            "meeting_password": passcode,
            "recording_token": recording_token,
            "display_name": display_name[:64],
            "output_path": str(output_path),
            "sdk_key": sdk_key,
        }
        job_path.write_text(json.dumps(job), encoding="utf-8")
        try:
            os.chmod(job_path, 0o600)
        except OSError:
            pass

        log_path = Path("/var/log/multistream/zoom-sdk-recorder.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
        proc = subprocess.Popen(
            [str(self._binary), "--job", str(job_path)],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        session = RecorderSession(
            meeting_id=digits,
            output_path=output_path,
            display_name=display_name,
            pid=proc.pid,
            started_at=started,
        )
        self._write_state(
            {
                "pid": proc.pid,
                "meeting_id": digits,
                "display_name": display_name,
                "output_path": str(output_path),
                "job_path": str(job_path),
                "started_at": started,
            }
        )
        return session

    def stop(self) -> RecorderResult:
        state = self._read_state()
        pid = state.get("pid")
        output = Path(state["output_path"]) if state.get("output_path") else None
        if pid:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
            # The wrapper leaves the meeting, then finishes the encode before it
            # exits, so this wait is what makes the file playable.
            for _ in range(STOP_TIMEOUT_SECONDS):
                try:
                    os.kill(int(pid), 0)
                except OSError:
                    break
                time.sleep(1)
            else:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass
        self._write_state({})
        if output and output.exists() and output.stat().st_size > 0:
            return RecorderResult(ok=True, output_path=output, message="Recording saved locally.")
        return RecorderResult(
            ok=False,
            output_path=output if output and output.exists() else None,
            message="Recorder stopped but no usable media file was produced.",
        )

    def status(self) -> dict:
        state = self._read_state()
        state["running"] = self.is_running()
        state["binary"] = str(self._binary)
        state["binary_present"] = self._binary.exists()
        return state

    def _read_state(self) -> dict:
        if not STATE_FILE.exists():
            return {}
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_state(self, payload: dict) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(STATE_FILE, 0o600)
        except OSError:
            pass
