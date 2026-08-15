"""Persist recording config on the VM (not Key Vault — schedule changes often)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import RecordingConfig

DEFAULT_PATH = Path(
    os.environ.get(
        "RECORDING_CONFIG_PATH",
        "/opt/multistream/etc/recording-schedule.json",
    )
)


def load(path: Path | None = None) -> RecordingConfig:
    target = path or DEFAULT_PATH
    if not target.exists():
        return RecordingConfig()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return RecordingConfig()
    if not isinstance(raw, dict):
        return RecordingConfig()
    return RecordingConfig.from_dict(raw)


def save(config: RecordingConfig, path: Path | None = None) -> None:
    target = path or DEFAULT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".recording-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, target)
        try:
            os.chmod(target, 0o640)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
