"""Start and stop Zoom Custom Live Streaming through the Zoom REST API.

This is what lets a phone-only host go live: instead of someone clicking
"Live on Custom Live Streaming Service" in desktop Zoom, we point the meeting
at the MultiStream RTMP ingest and flip the livestream status from here.

Requires a Zoom Server-to-Server OAuth app on the account that owns the
meeting, with these scopes:
    meeting:read:meeting:admin
    meeting:write:meeting:admin
    meeting:read:livestream:admin
    meeting:update:livestream:admin
    meeting:update:livestream_status:admin

Key Vault secrets: zoom-account-id, zoom-client-id, zoom-client-secret
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

GetSecret = Callable[[str], str]

TOKEN_URL = "https://zoom.us/oauth/token"
API = "https://api.zoom.us/v2"

_lock = threading.Lock()
_token: dict[str, Any] = {}


class ZoomError(Exception):
    """User-visible Zoom API failure."""


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict | None = None,
    timeout: int = 30,
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
            return exc.code, json.loads(detail)
        except json.JSONDecodeError:
            return exc.code, detail
    except urllib.error.URLError as exc:
        raise ZoomError(f"Network error calling Zoom: {exc}") from exc


def _optional(get_secret: GetSecret, name: str) -> str | None:
    try:
        value = get_secret(name).strip()
    except Exception:  # noqa: BLE001
        return None
    return value or None


def configured(get_secret: GetSecret) -> bool:
    return all(
        _optional(get_secret, name)
        for name in ("zoom-account-id", "zoom-client-id", "zoom-client-secret")
    )


def access_token(get_secret: GetSecret) -> str:
    with _lock:
        if _token.get("value") and time.time() < float(_token.get("expires", 0)) - 120:
            return str(_token["value"])

    account_id = _optional(get_secret, "zoom-account-id")
    client_id = _optional(get_secret, "zoom-client-id")
    client_secret = _optional(get_secret, "zoom-client-secret")
    if not account_id or not client_id or not client_secret:
        raise ZoomError(
            "Zoom API is not configured. Save the Server-to-Server OAuth "
            "Account ID, Client ID and Client Secret first."
        )

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    status, payload = _request(
        "POST",
        f"{TOKEN_URL}?grant_type=account_credentials"
        f"&account_id={urllib.parse.quote(account_id)}",
        headers={"Authorization": f"Basic {auth}"},
    )
    if status >= 400 or not isinstance(payload, dict) or not payload.get("access_token"):
        raise ZoomError(f"Zoom rejected the app credentials ({status}): {payload}")

    token = str(payload["access_token"])
    with _lock:
        _token["value"] = token
        _token["expires"] = time.time() + float(payload.get("expires_in") or 3600)
    return token


def _api(
    get_secret: GetSecret,
    method: str,
    path: str,
    body: dict | None = None,
) -> tuple[int, dict | str]:
    token = access_token(get_secret)
    return _request(
        method,
        f"{API}{path}",
        headers={"Authorization": f"Bearer {token}"},
        body=body,
    )


def normalize_meeting_id(raw: str) -> str:
    """Accept '891 2345 6789', a join URL, or a bare ID."""
    value = (raw or "").strip()
    if not value:
        raise ZoomError("Enter the Zoom meeting ID.")
    if "zoom" in value and "/" in value:
        path = urllib.parse.urlparse(value).path
        value = path.rstrip("/").rsplit("/", 1)[-1]
    digits = "".join(ch for ch in value if ch.isdigit())
    if not (9 <= len(digits) <= 11):
        raise ZoomError(f"“{raw.strip()}” does not look like a Zoom meeting ID.")
    return digits


def _explain(status: int, payload: dict | str, action: str) -> str:
    code = payload.get("code") if isinstance(payload, dict) else None
    message = payload.get("message") if isinstance(payload, dict) else str(payload)

    if status == 404:
        return (
            f"Zoom could not find that meeting ({message}). The meeting must be "
            "hosted on the same Zoom account as the API app."
        )
    if code == 4927 or status == 400:
        return (
            f"Zoom refused to {action} the livestream: {message}. The meeting "
            "usually has to be in progress before streaming can start."
        )
    if status in (401, 403):
        return (
            f"Zoom denied the request ({message}). Check the Server-to-Server app "
            "scopes and that Custom Live Streaming is enabled on the account."
        )
    return f"Zoom {action} failed ({status}): {message}"


def configure_livestream(
    get_secret: GetSecret,
    meeting_id: str,
    *,
    stream_url: str,
    stream_key: str,
    page_url: str,
    resolution: str = "720p",
) -> None:
    status, payload = _api(
        get_secret,
        "PATCH",
        f"/meetings/{urllib.parse.quote(meeting_id)}/livestream",
        {
            "stream_url": stream_url,
            "stream_key": stream_key,
            "page_url": page_url,
            "resolution": resolution,
        },
    )
    if status >= 400:
        raise ZoomError(_explain(status, payload, "configure"))


def start_livestream(
    get_secret: GetSecret,
    meeting_id: str,
    *,
    display_name: str = "ISKCON Deoghar",
) -> None:
    status, payload = _api(
        get_secret,
        "PATCH",
        f"/meetings/{urllib.parse.quote(meeting_id)}/livestream/status",
        {
            "action": "start",
            "settings": {
                "active_speaker_name": True,
                "display_name": display_name[:64],
            },
        },
    )
    if status >= 400:
        raise ZoomError(_explain(status, payload, "start"))


def stop_livestream(get_secret: GetSecret, meeting_id: str) -> None:
    status, payload = _api(
        get_secret,
        "PATCH",
        f"/meetings/{urllib.parse.quote(meeting_id)}/livestream/status",
        {"action": "stop"},
    )
    if status >= 400:
        raise ZoomError(_explain(status, payload, "stop"))
