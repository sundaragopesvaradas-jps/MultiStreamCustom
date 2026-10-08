#!/usr/bin/env python3
"""Complete YouTube and end Facebook lives after Zoom has stayed idle.

Called after the stream-end grace period. Safe to re-run (idempotent).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

UI_DIR = Path("/opt/multistream/ui")
sys.path.insert(0, str(UI_DIR))

import keyvault  # noqa: E402
import platforms  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s end-lives %(levelname)s %(message)s",
)
log = logging.getLogger("end-lives")

CONFIG_ENV = Path("/opt/multistream/etc/multistream.env")


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


def main() -> int:
    if not _kv_name():
        log.error("KEY_VAULT_NAME is not set")
        return 1

    errors = 0
    try:
        msg = platforms.youtube_complete_broadcast(get_secret, set_secret)
        log.info("%s", msg)
    except Exception as exc:  # noqa: BLE001
        errors += 1
        log.error("YouTube complete failed: %s", exc)

    try:
        msg = platforms.facebook_end_live(get_secret)
        log.info("%s", msg)
    except Exception as exc:  # noqa: BLE001
        errors += 1
        log.error("Facebook end failed: %s", exc)

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
