#!/usr/bin/env python3
"""If Zoom goes live without a fresh Prepare live, create platform lives now.

Uses the fixed default title/description. Mid-stream title edits still go
through the UI "Update title & description" button.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UI_DIR = Path("/opt/multistream/ui")
sys.path.insert(0, str(UI_DIR))

import keyvault  # noqa: E402
import platforms  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s auto-prepare %(levelname)s %(message)s",
)
log = logging.getLogger("auto-prepare")

ENABLED_FILE = Path("/opt/multistream/etc/enabled.env")
CONFIG_ENV = Path("/opt/multistream/etc/multistream.env")
SYNC = Path("/opt/multistream/bin/sync-secrets.sh")


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def _kv_name() -> str:
    return os.environ.get("KEY_VAULT_NAME") or _load_env(CONFIG_ENV).get("KEY_VAULT_NAME", "")


def get_secret(name: str) -> str:
    return keyvault.get_secret(_kv_name(), name)


def set_secret(name: str, value: str) -> None:
    keyvault.set_secret(_kv_name(), name, value)


def get_optional(name: str) -> str | None:
    return platforms.get_optional(get_secret, name)


def read_enabled() -> dict[str, bool]:
    values = _load_env(ENABLED_FILE)
    return {
        "youtube": values.get("YOUTUBE_ENABLED", "1") == "1",
        "facebook": values.get("FACEBOOK_ENABLED", "1") == "1",
    }


def main() -> int:
    if not _kv_name():
        log.error("KEY_VAULT_NAME is not set")
        return 1

    enabled = read_enabled()
    status = platforms.prepare_readiness(
        get_secret,
        set_secret,
        youtube_enabled=enabled["youtube"],
        facebook_enabled=enabled["facebook"],
        verify_apis=True,
    )
    if status.ready:
        log.info("lives already ready — skipping auto-prepare (%s)", status.message)
        return 0

    title, description = platforms.default_live_metadata(get_secret)
    log.warning(
        "lives not ready (%s) — auto-preparing with default title %r",
        status.message,
        title,
    )

    set_secret("stream-title", title)
    set_secret("stream-description", description)

    jobs = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        if enabled["youtube"] and status.youtube.needed and not status.youtube.ready:
            jobs["youtube"] = pool.submit(
                platforms.youtube_prepare_live, get_secret, set_secret, title, description
            )
        if enabled["facebook"] and status.facebook.needed and not status.facebook.ready:
            jobs["facebook"] = pool.submit(
                platforms.facebook_prepare_live, get_secret, set_secret, title, description
            )

    ok = False
    for name, job in jobs.items():
        try:
            result = job.result()
            log.info("%s prepared: %s", name, result.watch_url)
            ok = True
        except Exception as exc:  # noqa: BLE001
            log.error("%s auto-prepare failed: %s", name, exc)

    if not ok:
        return 1

    subprocess.run([str(SYNC)], check=True)
    log.info("auto-prepare complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
