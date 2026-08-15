#!/usr/bin/env python3
"""CLI entry for the recording scheduler.

  python -m recording tick     # one minute check (systemd timer)
  python -m recording purge    # delete blobs older than 6 months
  python -m recording status   # print config + recorder state
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_UI = _ROOT / "ui"
if _UI.is_dir():
    sys.path.insert(0, str(_UI))
sys.path.insert(0, str(_ROOT))

from recording import pipeline, store  # noqa: E402
from recording.pipeline import _secret_helpers  # noqa: E402
from recording.recorder import ZoomSdkRecorder  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="ISKCON scheduled Zoom recorder")
    parser.add_argument("command", choices=("tick", "purge", "status"))
    args = parser.parse_args(argv)

    if args.command == "tick":
        print(json.dumps(pipeline.tick(), indent=2))
        return 0
    if args.command == "purge":
        print(json.dumps({"deleted": pipeline.purge_retention()}, indent=2))
        return 0

    config = store.load()
    get_secret, get_optional, _ = _secret_helpers()
    recorder = ZoomSdkRecorder(get_secret, get_optional)
    print(
        json.dumps(
            {"config": config.to_dict(), "recorder": recorder.status()},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
