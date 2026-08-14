#!/usr/bin/env python3
"""Append one destination result to the MultiStream session history.

Stream keys are scrubbed from any log text before it is stored, because the
history is rendered in the web UI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

HISTORY_FILE = Path("/var/log/multistream/history.jsonl")
DEST_ENV = Path("/opt/multistream/etc/destinations.env")
MAX_RECORDS = 500
ERROR_SNIPPET_LINES = 12

# FFmpeg lines worth surfacing; everything else is noise for a non-expert reader.
INTERESTING = re.compile(
    r"(error|failed|refused|unauthorized|forbidden|timed? out|timeout|"
    r"broken pipe|not found|invalid|denied|unable|cannot|closed|end of file)",
    re.IGNORECASE,
)

# Progress and shutdown chatter that appears on every normal stream end.
BENIGN = re.compile(
    r"(^frame=|^size=|^video:|Input/output error|Failed to update header|"
    r"Exiting normally|Received signal|muxing overhead|Qavg)",
    re.IGNORECASE,
)


def load_secrets() -> list[str]:
    secrets: list[str] = []
    if not DEST_ENV.exists():
        return secrets
    for line in DEST_ENV.read_text().splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'").strip('"')
        if not value:
            continue
        if key.strip().endswith("_STREAM_KEY"):
            secrets.append(value)
        elif key.strip().endswith("_RTMP_URL"):
            tail = value.rstrip("/").rsplit("/", 1)[-1]
            if tail:
                secrets.append(tail)
    return sorted(set(secrets), key=len, reverse=True)


def scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


def read_progress(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def extract_errors(log_path: Path, secrets: list[str]) -> list[str]:
    """Return only lines a human would need; a clean run yields nothing."""
    if not log_path.exists():
        return []
    hits = [
        line.strip()
        for line in log_path.read_text(errors="replace").splitlines()
        if line.strip() and INTERESTING.search(line) and not BENIGN.search(line.strip())
    ]
    return [scrub(line, secrets) for line in hits[-ERROR_SNIPPET_LINES:]]


def classify(exit_code: int, duration: float, total_bytes: int, errors: list[str]) -> str:
    """Map raw FFmpeg outcome onto something a human can act on."""
    hard_failure = any(
        re.search(r"(refused|unauthorized|forbidden|denied|not found|invalid)", line, re.I)
        for line in errors
    )
    if total_bytes <= 0:
        return "failed" if hard_failure or duration < 10 else "no_data"
    if hard_failure and duration < 10:
        return "failed"
    if duration < 10:
        return "dropped_early"
    # SIGTERM/SIGKILL from a normal stream end still means we delivered.
    return "delivered"


def trim_history() -> None:
    if not HISTORY_FILE.exists():
        return
    lines = HISTORY_FILE.read_text(errors="replace").splitlines()
    if len(lines) > MAX_RECORDS:
        HISTORY_FILE.write_text("\n".join(lines[-MAX_RECORDS:]) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--path", default="")
    parser.add_argument("--started", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--log-file", required=True)
    parser.add_argument("--progress-file", default="")
    args = parser.parse_args()

    secrets = load_secrets()
    log_path = Path(args.log_file)
    progress = read_progress(Path(args.progress_file)) if args.progress_file else {}

    ended = datetime.now(timezone.utc)
    try:
        started = datetime.fromisoformat(args.started.replace("Z", "+00:00"))
    except ValueError:
        started = ended
    duration = max(0.0, (ended - started).total_seconds())

    try:
        total_bytes = int(progress.get("total_size", "0"))
    except ValueError:
        total_bytes = 0

    errors = extract_errors(log_path, secrets)
    status = classify(args.exit_code, duration, total_bytes, errors)

    record = {
        "session": args.session,
        "destination": args.destination,
        "path": args.path,
        "started": started.isoformat().replace("+00:00", "Z"),
        "ended": ended.isoformat().replace("+00:00", "Z"),
        "duration_sec": round(duration, 1),
        "exit_code": args.exit_code,
        "bytes": total_bytes,
        "bitrate": progress.get("bitrate", ""),
        "status": status,
        "errors": errors,
    }

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    os.chmod(HISTORY_FILE, 0o600)
    trim_history()
    print(f"recorded {args.destination}: {status} ({total_bytes} bytes, {duration:.0f}s)")


if __name__ == "__main__":
    main()
