#!/usr/bin/env python3
"""PoC: start Zoom Custom Live Streaming via REST API (no desktop click).

Hypothesis
----------
If the host is only on Zoom mobile, can we still start Custom RTMP by calling:

  PATCH /meetings/{id}/livestream          (set Azure URL + key)
  PATCH /meetings/{id}/livestream/status   (action=start)

If Azure MediaMTX then receives an RTMP publisher, the API path works without
an encoder bot. If start succeeds but no RTMP arrives (or start fails), Zoom
still requires a desktop/SDK client to push — and we need Option B encoding.

Prerequisites
-------------
1. Zoom Marketplace → Server-to-Server OAuth app with scopes:
     meeting:write:meeting:admin
     meeting:update:livestream:admin
     meeting:update:livestream_status:admin
     meeting:read:livestream:admin
     meeting:read:meeting:admin
2. Account: Custom Live Streaming Service enabled
3. Store in Key Vault (or export env):
     zoom-account-id, zoom-client-id, zoom-client-secret
4. Host has started the Zoom meeting (phone is fine for join). Meeting must
   already be in progress before --start.

Usage
-----
  # Configure RTMP target for a meeting (can be before or during meeting)
  python3 zoom-livestream-poc.py configure --meeting-id 123456789

  # While meeting is live on phone only — try to start
  python3 zoom-livestream-poc.py start --meeting-id 123456789

  # Check Zoom + local MediaMTX
  python3 zoom-livestream-poc.py status --meeting-id 123456789

  # Stop
  python3 zoom-livestream-poc.py stop --meeting-id 123456789
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_ENV = Path("/opt/multistream/etc/multistream.env")
UI_DIR = Path("/opt/multistream/ui")
MEDIAMTX_API = "http://127.0.0.1:9997/v3/paths/list"
ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
ZOOM_API = "https://api.zoom.us/v2"


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'").strip('"')
    return out


def _kv_name() -> str:
    return os.environ.get("KEY_VAULT_NAME") or _load_env_file(CONFIG_ENV).get("KEY_VAULT_NAME", "")


def _secret(name: str) -> str | None:
    env_key = name.upper().replace("-", "_")
    if os.environ.get(env_key):
        return os.environ[env_key]
    # Prefer Key Vault REST helper when running on the VM.
    if UI_DIR.exists():
        sys.path.insert(0, str(UI_DIR))
        try:
            import keyvault  # type: ignore

            return keyvault.get_secret(_kv_name(), name)
        except Exception:
            pass
    try:
        vault = _kv_name()
        if not vault:
            return None
        result = subprocess.run(
            [
                "az",
                "keyvault",
                "secret",
                "show",
                "--vault-name",
                vault,
                "--name",
                name,
                "--query",
                "value",
                "-o",
                "tsv",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict | None = None,
    timeout: int = 45,
) -> tuple[int, dict | str]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req_headers = dict(headers or {})
    if body is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return resp.status, {}
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            parsed = detail
        return exc.code, parsed


def zoom_access_token() -> str:
    account_id = _secret("zoom-account-id")
    client_id = _secret("zoom-client-id")
    client_secret = _secret("zoom-client-secret")
    if not account_id or not client_id or not client_secret:
        raise SystemExit(
            "Missing Zoom Server-to-Server OAuth secrets.\n"
            "Store zoom-account-id, zoom-client-id, zoom-client-secret in Key Vault\n"
            "or export ZOOM_ACCOUNT_ID / ZOOM_CLIENT_ID / ZOOM_CLIENT_SECRET."
        )
    import base64

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    status, payload = _http_json(
        "POST",
        f"{ZOOM_TOKEN_URL}?grant_type=account_credentials&account_id={urllib.parse.quote(account_id)}",
        headers={"Authorization": f"Basic {auth}"},
    )
    if status >= 400 or not isinstance(payload, dict) or not payload.get("access_token"):
        raise SystemExit(f"Zoom token failed ({status}): {payload}")
    return str(payload["access_token"])


def zoom_api(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict | str]:
    return _http_json(
        method,
        f"{ZOOM_API}{path}",
        headers={"Authorization": f"Bearer {token}"},
        body=body,
    )


def ingest_target() -> tuple[str, str, str]:
    """Return (stream_url, stream_key, page_url) for MultiStream."""
    cfg = _load_env_file(CONFIG_ENV)
    host = (
        os.environ.get("PUBLIC_HOST")
        or cfg.get("PUBLIC_HOST")
        or "multistream-jixozpwkde4wu.centralindia.cloudapp.azure.com"
    )
    # Prefer FQDN for Zoom (some clients dislike raw IP RTMP).
    if host.replace(".", "").isdigit():
        host = "multistream-jixozpwkde4wu.centralindia.cloudapp.azure.com"
    key = _secret("ingest-stream-key")
    if not key:
        raise SystemExit("ingest-stream-key missing from Key Vault")
    stream_url = f"rtmp://{host}/live"
    page_url = f"https://{host}/"
    return stream_url, key, page_url


def mediamtx_publishers() -> list[dict]:
    try:
        status, payload = _http_json("GET", MEDIAMTX_API)
    except Exception as exc:  # noqa: BLE001
        print(f"MediaMTX API unreachable: {exc}")
        return []
    if status >= 400 or not isinstance(payload, dict):
        print(f"MediaMTX API error ({status}): {payload}")
        return []
    items = payload.get("items") or []
    return [i for i in items if i.get("ready")]


def cmd_configure(meeting_id: str) -> None:
    token = zoom_access_token()
    stream_url, stream_key, page_url = ingest_target()
    print(f"Configuring meeting {meeting_id}")
    print(f"  stream_url = {stream_url}")
    print(f"  stream_key = {stream_key[:4]}…{stream_key[-4:]} (len={len(stream_key)})")
    print(f"  page_url   = {page_url}")
    status, payload = zoom_api(
        "PATCH",
        f"/meetings/{urllib.parse.quote(meeting_id)}/livestream",
        token,
        {
            "stream_url": stream_url,
            "stream_key": stream_key,
            "page_url": page_url,
            "resolution": "720p",
        },
    )
    print(f"configure → HTTP {status}: {payload or '(empty = success)'}")
    if status >= 400:
        raise SystemExit(1)


def cmd_start(meeting_id: str) -> None:
    token = zoom_access_token()
    print(f"Starting livestream for meeting {meeting_id} …")
    before = mediamtx_publishers()
    print(f"MediaMTX ready paths before: {len(before)}")
    status, payload = zoom_api(
        "PATCH",
        f"/meetings/{urllib.parse.quote(meeting_id)}/livestream/status",
        token,
        {
            "action": "start",
            "settings": {
                "active_speaker_name": True,
                "display_name": "MultiStream PoC",
            },
        },
    )
    print(f"start → HTTP {status}: {payload or '(empty = success)'}")
    if status >= 400:
        raise SystemExit(
            "Start failed. Common causes:\n"
            "  - meeting is not in progress yet\n"
            "  - Custom Live Streaming not enabled on the account\n"
            "  - missing livestream scopes on the S2S app\n"
            "  - Zoom still requires a desktop client to push RTMP"
        )

    print("Waiting up to 45s for MediaMTX to see an RTMP publisher …")
    deadline = time.time() + 45
    while time.time() < deadline:
        ready = mediamtx_publishers()
        if ready:
            print("RESULT: PASS — Zoom is publishing RTMP to MultiStream")
            for item in ready:
                print(f"  path={item.get('name')} readers={len(item.get('readers') or [])}")
            return
        time.sleep(3)
    print(
        "RESULT: FAIL — Zoom accepted start, but no RTMP arrived at MediaMTX.\n"
        "Interpretation: API start alone is not enough with a mobile-only host;\n"
        "Zoom likely still needs a desktop/SDK client to egress RTMP.\n"
        "Next option: Meeting SDK bot (true Option B) or co-host laptop."
    )
    raise SystemExit(2)


def cmd_stop(meeting_id: str) -> None:
    token = zoom_access_token()
    status, payload = zoom_api(
        "PATCH",
        f"/meetings/{urllib.parse.quote(meeting_id)}/livestream/status",
        token,
        {"action": "stop"},
    )
    print(f"stop → HTTP {status}: {payload or '(empty = success)'}")
    if status >= 400:
        raise SystemExit(1)


def cmd_status(meeting_id: str) -> None:
    token = zoom_access_token()
    status, payload = zoom_api(
        "GET",
        f"/meetings/{urllib.parse.quote(meeting_id)}/livestream",
        token,
    )
    print(f"Zoom livestream details → HTTP {status}")
    print(json.dumps(payload, indent=2) if isinstance(payload, dict) else payload)
    ready = mediamtx_publishers()
    print(f"MediaMTX ready publishers: {len(ready)}")
    for item in ready:
        print(f"  path={item.get('name')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Zoom Custom RTMP livestream API PoC")
    parser.add_argument("command", choices=("configure", "start", "stop", "status"))
    parser.add_argument("--meeting-id", required=True, help="Zoom meeting ID (numeric)")
    args = parser.parse_args()

    if args.command == "configure":
        cmd_configure(args.meeting_id)
    elif args.command == "start":
        cmd_start(args.meeting_id)
    elif args.command == "stop":
        cmd_stop(args.meeting_id)
    else:
        cmd_status(args.meeting_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
