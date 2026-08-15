"""YouTube + Facebook live metadata via OAuth APIs.

Title/description cannot ride on RTMP. This module creates/updates live
objects on each platform and returns ingest stream keys for the relay.
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

GetSecret = Callable[[str], str]
SetSecret = Callable[[str, str], None]
GetOptional = Callable[[str], str | None]

# Shown in the UI form when nothing has been saved yet, and used by
# auto-prepare when Zoom goes live without a fresh Prepare live — unless
# Key Vault secrets default-stream-title / default-stream-description are set.
DEFAULT_LIVE_TITLE = "ISKCON Deoghar Live"
DEFAULT_LIVE_DESCRIPTION = "ISKCON Deoghar Live"


def default_live_metadata(get_secret: GetSecret) -> tuple[str, str]:
    """Title/description used when Zoom starts without Prepare live."""
    title = get_optional(get_secret, "default-stream-title") or DEFAULT_LIVE_TITLE
    description = get_optional(get_secret, "default-stream-description") or DEFAULT_LIVE_DESCRIPTION
    return title.strip() or DEFAULT_LIVE_TITLE, description.strip() or DEFAULT_LIVE_DESCRIPTION


# YouTube lifeCycleStatus values that still accept an RTMP push.
_YT_ACCEPTING = frozenset({"ready", "testing", "live"})
# Facebook Live Video status values that still accept an RTMP push.
_FB_ACCEPTING = frozenset({"UNPUBLISHED", "LIVE_NOW", "SCHEDULED_UNPUBLISHED", "SCHEDULED_LIVE"})


class PlatformError(Exception):
    """User-visible API failure."""


@dataclass
class LivePrepResult:
    platform: str
    stream_key: str
    watch_url: str
    broadcast_id: str = ""


@dataclass
class DestinationReadiness:
    needed: bool
    ready: bool
    detail: str


@dataclass
class PrepareReadiness:
    """Whether enabled destinations still have usable live objects."""

    ready: bool
    needs_prepare: bool
    youtube: DestinationReadiness
    facebook: DestinationReadiness
    message: str


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 45,
) -> dict[str, Any]:
    data: bytes | None = None
    req_headers = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            detail = json.dumps(parsed.get("error", parsed))[:800]
        except json.JSONDecodeError:
            detail = detail[:800]
        raise PlatformError(f"{method} {url.split('?', 1)[0]} failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise PlatformError(f"Network error calling platform API: {exc}") from exc


def public_base_url(public_host: str) -> str:
    host = (public_host or "").strip()
    if host.startswith("http://") or host.startswith("https://"):
        return host.rstrip("/")
    # Prefer HTTPS FQDN when we only have an IP in PUBLIC_HOST.
    if host.replace(".", "").isdigit() or ":" in host:
        return f"https://multistream-jixozpwkde4wu.centralindia.cloudapp.azure.com"
    return f"https://{host}"


# --- credential helpers -------------------------------------------------


def get_optional(get_secret: GetSecret, name: str) -> str | None:
    try:
        value = get_secret(name).strip()
    except Exception:  # noqa: BLE001
        return None
    if not value or value in {"REPLACE_ME", "MOVED_TO_HASH"}:
        return None
    return value


def oauth_configured(get_secret: GetSecret) -> dict[str, bool]:
    return {
        "google_app": bool(
            get_optional(get_secret, "google-oauth-client-id")
            and get_optional(get_secret, "google-oauth-client-secret")
        ),
        "facebook_app": bool(
            get_optional(get_secret, "facebook-app-id")
            and get_optional(get_secret, "facebook-app-secret")
        ),
        "youtube_connected": bool(get_optional(get_secret, "youtube-oauth-tokens")),
        "facebook_connected": bool(get_optional(get_secret, "facebook-page-token")),
    }


# --- YouTube -------------------------------------------------------------

YT_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
YT_TOKEN = "https://oauth2.googleapis.com/token"
YT_API = "https://www.googleapis.com/youtube/v3"
YT_SCOPES = " ".join(
    [
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/youtube.force-ssl",
    ]
)


def youtube_authorize_url(get_secret: GetSecret, public_host: str, state: str) -> str:
    client_id = get_optional(get_secret, "google-oauth-client-id")
    if not client_id:
        raise PlatformError("Google OAuth client ID is not configured in Key Vault.")
    redirect = f"{public_base_url(public_host)}/oauth/youtube/callback"
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": YT_SCOPES,
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{YT_AUTH}?{query}"


def youtube_exchange_code(
    get_secret: GetSecret,
    set_secret: SetSecret,
    public_host: str,
    code: str,
) -> None:
    client_id = get_optional(get_secret, "google-oauth-client-id")
    client_secret = get_optional(get_secret, "google-oauth-client-secret")
    if not client_id or not client_secret:
        raise PlatformError("Google OAuth app credentials missing.")
    redirect = f"{public_base_url(public_host)}/oauth/youtube/callback"
    payload = _http_json(
        "POST",
        YT_TOKEN,
        form={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
        },
    )
    if "refresh_token" not in payload:
        # Re-consent may omit refresh_token if one already exists — keep old.
        existing = get_optional(get_secret, "youtube-oauth-tokens")
        if existing:
            old = json.loads(existing)
            payload["refresh_token"] = old.get("refresh_token", "")
    if not payload.get("refresh_token"):
        raise PlatformError(
            "Google did not return a refresh token. Revoke MultiStream access in "
            "https://myaccount.google.com/permissions and connect again."
        )
    payload["obtained_at"] = int(time.time())
    set_secret("youtube-oauth-tokens", json.dumps(payload))


def _youtube_access_token(get_secret: GetSecret, set_secret: SetSecret) -> str:
    raw = get_optional(get_secret, "youtube-oauth-tokens")
    if not raw:
        raise PlatformError("YouTube is not connected. Use Connect YouTube first.")
    tokens = json.loads(raw)
    access = tokens.get("access_token", "")
    obtained = int(tokens.get("obtained_at", 0))
    expires_in = int(tokens.get("expires_in", 0))
    if access and obtained and time.time() < obtained + expires_in - 60:
        return access

    client_id = get_optional(get_secret, "google-oauth-client-id")
    client_secret = get_optional(get_secret, "google-oauth-client-secret")
    refresh = tokens.get("refresh_token")
    if not client_id or not client_secret or not refresh:
        raise PlatformError("YouTube OAuth tokens incomplete — reconnect YouTube.")

    refreshed = _http_json(
        "POST",
        YT_TOKEN,
        form={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        },
    )
    tokens["access_token"] = refreshed["access_token"]
    tokens["expires_in"] = refreshed.get("expires_in", 3600)
    tokens["obtained_at"] = int(time.time())
    if refreshed.get("refresh_token"):
        tokens["refresh_token"] = refreshed["refresh_token"]
    set_secret("youtube-oauth-tokens", json.dumps(tokens))
    return tokens["access_token"]


def youtube_prepare_live(
    get_secret: GetSecret,
    set_secret: SetSecret,
    title: str,
    description: str,
) -> LivePrepResult:
    token = _youtube_access_token(get_secret, set_secret)
    headers = {"Authorization": f"Bearer {token}"}
    start = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    broadcast = _http_json(
        "POST",
        f"{YT_API}/liveBroadcasts?part=snippet,status,contentDetails",
        headers=headers,
        body={
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "scheduledStartTime": start,
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
            "contentDetails": {
                "enableAutoStart": True,
                "enableAutoStop": True,
                "monitorStream": {"enableMonitorStream": False},
            },
        },
    )
    broadcast_id = broadcast["id"]

    stream = _http_json(
        "POST",
        f"{YT_API}/liveStreams?part=snippet,cdn",
        headers=headers,
        body={
            "snippet": {"title": f"MultiStream {start}"},
            "cdn": {
                "frameRate": "variable",
                "ingestionType": "rtmp",
                "resolution": "variable",
            },
        },
    )
    stream_id = stream["id"]
    ingestion = stream.get("cdn", {}).get("ingestionInfo", {})
    stream_key = ingestion.get("streamName", "")
    if not stream_key:
        raise PlatformError("YouTube created a stream but returned no stream key.")

    _http_json(
        "POST",
        f"{YT_API}/liveBroadcasts/bind?id={urllib.parse.quote(broadcast_id)}"
        f"&streamId={urllib.parse.quote(stream_id)}&part=id,contentDetails,snippet",
        headers={**headers, "Content-Length": "0"},
    )

    watch_url = f"https://youtu.be/{broadcast_id}"
    set_secret("youtube-stream-key", stream_key)
    set_secret("youtube-watch-url", watch_url)
    set_secret("youtube-broadcast-id", broadcast_id)
    set_secret("lives-prepared-at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return LivePrepResult(
        platform="youtube",
        stream_key=stream_key,
        watch_url=watch_url,
        broadcast_id=broadcast_id,
    )


def youtube_broadcast_accepting(get_secret: GetSecret, set_secret: SetSecret) -> DestinationReadiness:
    """True when the stored YouTube broadcast can still take an RTMP push."""
    if not get_optional(get_secret, "youtube-oauth-tokens"):
        return DestinationReadiness(True, False, "YouTube is not connected.")
    broadcast_id = get_optional(get_secret, "youtube-broadcast-id")
    if not broadcast_id:
        return DestinationReadiness(True, False, "No YouTube broadcast prepared yet.")
    try:
        token = _youtube_access_token(get_secret, set_secret)
        current = _http_json(
            "GET",
            f"{YT_API}/liveBroadcasts?part=status&id={urllib.parse.quote(broadcast_id)}",
            headers={"Authorization": f"Bearer {token}"},
        )
    except PlatformError as exc:
        return DestinationReadiness(True, False, f"Could not check YouTube: {exc}")
    items = current.get("items") or []
    if not items:
        return DestinationReadiness(True, False, "Stored YouTube broadcast no longer exists.")
    status = (items[0].get("status") or {}).get("lifeCycleStatus", "")
    if status in _YT_ACCEPTING:
        return DestinationReadiness(True, True, f"YouTube broadcast is {status}.")
    return DestinationReadiness(
        True, False, f"YouTube broadcast is {status or 'unknown'} — prepare a new one."
    )


def youtube_update_metadata(
    get_secret: GetSecret,
    set_secret: SetSecret,
    title: str,
    description: str,
) -> str:
    """Retitle the existing broadcast without touching its stream key."""
    broadcast_id = get_optional(get_secret, "youtube-broadcast-id")
    if not broadcast_id:
        raise PlatformError("No YouTube broadcast yet — use Prepare live first.")

    token = _youtube_access_token(get_secret, set_secret)
    headers = {"Authorization": f"Bearer {token}"}

    current = _http_json(
        "GET",
        f"{YT_API}/liveBroadcasts?part=snippet&id={urllib.parse.quote(broadcast_id)}",
        headers=headers,
    )
    items = current.get("items") or []
    if not items:
        raise PlatformError(
            "That YouTube broadcast no longer exists. Run Prepare live to create a new one."
        )
    snippet = items[0].get("snippet", {})

    # YouTube replaces the whole snippet on update, so scheduledStartTime has
    # to be sent back or the request is rejected.
    body_snippet = {
        "title": title[:100],
        "description": description[:5000],
    }
    if snippet.get("scheduledStartTime"):
        body_snippet["scheduledStartTime"] = snippet["scheduledStartTime"]

    _http_json(
        "PUT",
        f"{YT_API}/liveBroadcasts?part=snippet,id",
        headers=headers,
        body={"id": broadcast_id, "snippet": body_snippet},
    )
    return f"https://youtu.be/{broadcast_id}"


def facebook_update_metadata(
    get_secret: GetSecret,
    title: str,
    description: str,
) -> str:
    """Retitle the existing live video without touching its stream key."""
    live_id = get_optional(get_secret, "facebook-live-id")
    page_token = get_optional(get_secret, "facebook-page-token")
    if not live_id:
        raise PlatformError("No Facebook live yet — use Prepare live first.")
    if not page_token:
        raise PlatformError("Facebook Page is not connected.")

    try:
        _http_json(
            "POST",
            f"{_fb_graph(get_secret)}/{live_id}",
            form={
                "title": title[:255],
                "description": description[:5000],
                "access_token": page_token,
            },
        )
    except PlatformError as exc:
        raise PlatformError(_explain_facebook_error(str(exc))) from exc
    return get_optional(get_secret, "facebook-watch-url") or ""


# --- Facebook ------------------------------------------------------------

FB_DEFAULT_VERSION = "v23.0"
# Page broadcasting needs only these; publish_video is for User-profile lives
# and asking for it triggers an App Review requirement we do not need.
FB_SCOPES = ",".join(
    [
        "pages_show_list",
        "pages_read_engagement",
        "pages_manage_posts",
    ]
)


def _fb_version(get_secret: GetSecret) -> str:
    return get_optional(get_secret, "facebook-graph-version") or FB_DEFAULT_VERSION


def _fb_graph(get_secret: GetSecret) -> str:
    return f"https://graph.facebook.com/{_fb_version(get_secret)}"


def facebook_authorize_url(get_secret: GetSecret, public_host: str, state: str) -> str:
    app_id = get_optional(get_secret, "facebook-app-id")
    if not app_id:
        raise PlatformError("Facebook App ID is not configured in Key Vault.")
    redirect = f"{public_base_url(public_host)}/oauth/facebook/callback"
    params = {
        "client_id": app_id,
        "redirect_uri": redirect,
        "state": state,
        "response_type": "code",
    }
    # Facebook Login for Business apps use a saved configuration instead of scopes.
    config_id = get_optional(get_secret, "facebook-login-config-id")
    if config_id:
        params["config_id"] = config_id
    else:
        params["scope"] = FB_SCOPES
    query = urllib.parse.urlencode(params)
    return f"https://www.facebook.com/{_fb_version(get_secret)}/dialog/oauth?{query}"


def facebook_exchange_code(
    get_secret: GetSecret,
    set_secret: SetSecret,
    public_host: str,
    code: str,
) -> str:
    """Exchange code, pick first Page, store page token. Returns page name."""
    app_id = get_optional(get_secret, "facebook-app-id")
    app_secret = get_optional(get_secret, "facebook-app-secret")
    if not app_id or not app_secret:
        raise PlatformError("Facebook app credentials missing.")
    graph = _fb_graph(get_secret)
    token_url = f"{graph}/oauth/access_token"
    redirect = f"{public_base_url(public_host)}/oauth/facebook/callback"
    short = _http_json(
        "GET",
        f"{token_url}?{urllib.parse.urlencode({'client_id': app_id, 'client_secret': app_secret, 'redirect_uri': redirect, 'code': code})}",
    )
    user_token = short.get("access_token")
    if not user_token:
        raise PlatformError("Facebook did not return a user access token.")

    long_lived = _http_json(
        "GET",
        f"{token_url}?{urllib.parse.urlencode({'grant_type': 'fb_exchange_token', 'client_id': app_id, 'client_secret': app_secret, 'fb_exchange_token': user_token})}",
    )
    long_token = long_lived.get("access_token") or user_token

    pages = _http_json(
        "GET",
        f"{graph}/me/accounts?fields=id,name,access_token&access_token={urllib.parse.quote(long_token)}",
    )
    data = pages.get("data") or []
    if not data:
        raise PlatformError(
            "No Facebook Pages were returned. The Live API needs a Page you administer, "
            "and the login must grant pages_show_list. If you used Facebook Login for "
            "Business, check that the configuration selects your Page and the "
            "pages_show_list, pages_read_engagement and pages_manage_posts permissions."
        )
    # Prefer previously selected page if still present.
    preferred = get_optional(get_secret, "facebook-page-id")
    page = next((p for p in data if preferred and p.get("id") == preferred), data[0])
    set_secret("facebook-user-token", long_token)
    set_secret("facebook-page-id", page["id"])
    set_secret("facebook-page-name", page.get("name", ""))
    set_secret("facebook-page-token", page["access_token"])
    set_secret(
        "facebook-pages-json",
        json.dumps([{"id": p["id"], "name": p.get("name", "")} for p in data]),
    )
    return page.get("name") or page["id"]


def facebook_prepare_live(
    get_secret: GetSecret,
    set_secret: SetSecret,
    title: str,
    description: str,
) -> LivePrepResult:
    page_id = get_optional(get_secret, "facebook-page-id")
    page_token = get_optional(get_secret, "facebook-page-token")
    if not page_id or not page_token:
        raise PlatformError("Facebook Page is not connected. Use Connect Facebook first.")

    try:
        live = _http_json(
            "POST",
            f"{_fb_graph(get_secret)}/{page_id}/live_videos",
            form={
                "title": title[:255],
                "description": description[:5000],
                "status": "LIVE_NOW",
                "access_token": page_token,
            },
        )
    except PlatformError as exc:
        raise PlatformError(_explain_facebook_error(str(exc))) from exc
    live_id = str(live.get("id", ""))
    stream_url = live.get("secure_stream_url") or live.get("stream_url") or ""
    if not stream_url or "/" not in stream_url:
        raise PlatformError(f"Facebook live created but no stream URL returned: {live}")
    stream_key = stream_url.rstrip("/").rsplit("/", 1)[-1]

    # The create response has no permalink, so ask for it separately. The link
    # only resolves publicly once the broadcast actually receives video data.
    permalink = live.get("permalink_url")
    if not permalink and live_id:
        try:
            detail = _http_json(
                "GET",
                f"{_fb_graph(get_secret)}/{live_id}"
                f"?fields=permalink_url&access_token={urllib.parse.quote(page_token)}",
            )
            permalink = detail.get("permalink_url")
        except PlatformError:
            permalink = None

    if permalink:
        watch_url = (
            f"https://www.facebook.com{permalink}"
            if permalink.startswith("/")
            else permalink
        )
    else:
        watch_url = f"https://www.facebook.com/{page_id}/videos/{live_id}/" if live_id else ""

    set_secret("facebook-stream-key", stream_key)
    # Keep the full URL: the API binds the key to the ingest host it returns,
    # and the key's query string must be passed through untouched.
    set_secret("facebook-stream-url", stream_url)
    set_secret("facebook-watch-url", watch_url)
    set_secret("facebook-live-id", live_id)
    set_secret("lives-prepared-at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return LivePrepResult(
        platform="facebook",
        stream_key=stream_key,
        watch_url=watch_url,
        broadcast_id=live_id,
    )


def facebook_live_accepting(get_secret: GetSecret) -> DestinationReadiness:
    """True when the stored Facebook live video can still take an RTMP push."""
    if not get_optional(get_secret, "facebook-page-token"):
        return DestinationReadiness(True, False, "Facebook is not connected.")
    live_id = get_optional(get_secret, "facebook-live-id")
    page_token = get_optional(get_secret, "facebook-page-token")
    if not live_id or not page_token:
        return DestinationReadiness(True, False, "No Facebook live prepared yet.")
    try:
        detail = _http_json(
            "GET",
            f"{_fb_graph(get_secret)}/{live_id}"
            f"?fields=status&access_token={urllib.parse.quote(page_token)}",
        )
    except PlatformError as exc:
        return DestinationReadiness(True, False, f"Could not check Facebook: {exc}")
    status = str(detail.get("status") or "")
    if status in _FB_ACCEPTING:
        return DestinationReadiness(True, True, f"Facebook live is {status}.")
    return DestinationReadiness(
        True, False, f"Facebook live is {status or 'unknown'} — prepare a new one."
    )


def prepare_readiness(
    get_secret: GetSecret,
    set_secret: SetSecret,
    *,
    youtube_enabled: bool,
    facebook_enabled: bool,
    verify_apis: bool = False,
) -> PrepareReadiness:
    """Decide whether Prepare live is still required before Zoom goes live.

    verify_apis=False is cheap (IDs + prepared-at age) for the UI banner.
    verify_apis=True hits YouTube/Facebook and is used by auto-prepare.
    """
    yt = DestinationReadiness(False, True, "YouTube destination is off.")
    fb = DestinationReadiness(False, True, "Facebook destination is off.")

    prepared_at = get_optional(get_secret, "lives-prepared-at")
    age_hours = None
    if prepared_at:
        try:
            when = datetime.fromisoformat(prepared_at.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - when).total_seconds() / 3600.0
        except ValueError:
            age_hours = None

    if youtube_enabled:
        if verify_apis:
            yt = youtube_broadcast_accepting(get_secret, set_secret)
        else:
            has_id = bool(get_optional(get_secret, "youtube-broadcast-id"))
            has_key = bool(get_optional(get_secret, "youtube-stream-key"))
            stale = age_hours is None or age_hours > 6
            if has_id and has_key and not stale:
                yt = DestinationReadiness(True, True, "YouTube looks prepared.")
            elif not has_id or not has_key:
                yt = DestinationReadiness(True, False, "YouTube has no prepared live yet.")
            else:
                yt = DestinationReadiness(
                    True, False, "Last Prepare live is older than 6 hours — create a fresh one."
                )

    if facebook_enabled:
        if verify_apis:
            fb = facebook_live_accepting(get_secret)
        else:
            has_id = bool(get_optional(get_secret, "facebook-live-id"))
            has_key = bool(get_optional(get_secret, "facebook-stream-key"))
            stale = age_hours is None or age_hours > 6
            if has_id and has_key and not stale:
                fb = DestinationReadiness(True, True, "Facebook looks prepared.")
            elif not has_id or not has_key:
                fb = DestinationReadiness(True, False, "Facebook has no prepared live yet.")
            else:
                fb = DestinationReadiness(
                    True, False, "Last Prepare live is older than 6 hours — create a fresh one."
                )

    needed = (yt.needed and not yt.ready) or (fb.needed and not fb.ready)
    ready = (not yt.needed or yt.ready) and (not fb.needed or fb.ready)
    if not youtube_enabled and not facebook_enabled:
        message = "Turn on at least one destination before going live."
        needed = True
        ready = False
    elif ready:
        message = "Lives look ready — Zoom can go live."
    else:
        parts = []
        if yt.needed and not yt.ready:
            parts.append(yt.detail)
        if fb.needed and not fb.ready:
            parts.append(fb.detail)
        message = " ".join(parts) or "Prepare live before Zoom."

    return PrepareReadiness(
        ready=ready,
        needs_prepare=needed,
        youtube=yt,
        facebook=fb,
        message=message,
    )


def _explain_facebook_error(detail: str) -> str:
    """Turn Meta's generic 'Permissions error' into the actual cause."""
    if "1363120" in detail:
        return (
            "Facebook rejected the broadcast: the account must be at least 60 days old "
            "before it can go live. " + detail
        )
    if "1363144" in detail:
        return (
            "Facebook rejected the broadcast: the Page needs at least 100 followers "
            "before it can go live. " + detail
        )
    if "live-video-api" in detail or "(#10)" in detail:
        return (
            "Facebook requires App Review for the Live Video API when acting on behalf of "
            "people who are not admins, developers or testers of your app. Keep the app in "
            "Development mode and add yourself as an admin/tester, or submit for review. "
            + detail
        )
    if "#200" in detail or "Permissions error" in detail:
        return (
            "Facebook permissions error. The Page token needs pages_read_engagement and "
            "pages_manage_posts, and you must be an admin of the Page. " + detail
        )
    return detail


def new_oauth_state() -> str:
    return secrets.token_urlsafe(24)
