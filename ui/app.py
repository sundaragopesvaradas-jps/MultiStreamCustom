#!/usr/bin/env python3
"""PIN-protected UI to update YouTube/Facebook stream keys in Azure Key Vault."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf import CSRFProtect
from flask_wtf.csrf import generate_csrf
from werkzeug.middleware.proxy_fix import ProxyFix

import keyvault
import notify
import platforms
import zoom

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(hours=2),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("HTTPS_ENABLED", "0") == "1",
    WTF_CSRF_TIME_LIMIT=None,
    # Token is enough; Referer checks break some browsers / privacy modes.
    WTF_CSRF_SSL_STRICT=False,
)

# Trust nginx X-Forwarded-* for client IP + HTTPS scheme
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

csrf = CSRFProtect(app)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

CONFIG_ENV = Path("/opt/multistream/etc/multistream.env")
HISTORY_FILE = Path("/var/log/multistream/history.jsonl")
RUN_DIR = Path("/opt/multistream/run")
ENABLED_FILE = Path("/opt/multistream/etc/enabled.env")
APPLY_SCRIPT = Path("/opt/multistream/bin/apply-destinations.sh")
MEDIAMTX_API = "http://127.0.0.1:9997/v3/paths/list"
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")
HASH_PREFIX = "pbkdf2_sha256"
HASH_ITERATIONS = 260000
IST = ZoneInfo("Asia/Kolkata")
LOGIN_LOCK_FILE = RUN_DIR / "login-lock.json"
LOGIN_FAIL_LIMIT = 5
LOGIN_LOCK_MINUTES = 60

STATUS_LABELS = {
    "delivered": "Delivered",
    "failed": "Failed to connect",
    "no_data": "Connected, no video sent",
    "dropped_early": "Dropped within 10s",
}

def load_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if CONFIG_ENV.exists():
        for line in CONFIG_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg

def kv_name() -> str:
    return os.environ.get("KEY_VAULT_NAME") or load_config().get("KEY_VAULT_NAME", "")

def get_secret(name: str) -> str:
    return keyvault.get_secret(kv_name(), name)

def set_secret(name: str, value: str) -> None:
    keyvault.set_secret(kv_name(), name, value)

def set_secrets(values: dict[str, str]) -> None:
    keyvault.set_secrets(kv_name(), values)

def get_secret_optional(name: str) -> str | None:
    return platforms.get_optional(get_secret, name)

# Everything the control page needs in one parallel Key Vault round-trip.
INDEX_SECRET_NAMES = (
    "ingest-stream-key",
    "youtube-stream-key",
    "facebook-stream-key",
    "google-oauth-client-id",
    "google-oauth-client-secret",
    "facebook-app-id",
    "facebook-app-secret",
    "facebook-login-config-id",
    "facebook-page-token",
    "facebook-page-name",
    "youtube-oauth-tokens",
    "default-stream-title",
    "default-stream-description",
    "stream-title",
    "stream-description",
    "youtube-watch-url",
    "facebook-watch-url",
    "lives-prepared-at",
    "youtube-broadcast-id",
    "facebook-live-id",
    "zoom-account-id",
    "zoom-client-id",
    "zoom-client-secret",
    "zoom-meeting-id",
    "smtp-user",
    "smtp-password",
)

# Manager console skips section 4 — fewer Key Vault reads on their page load.
MANAGER_SECRET_NAMES = (
    "youtube-stream-key",
    "facebook-stream-key",
    "default-stream-title",
    "default-stream-description",
    "stream-title",
    "stream-description",
    "youtube-watch-url",
    "facebook-watch-url",
    "lives-prepared-at",
    "youtube-broadcast-id",
    "facebook-live-id",
    "zoom-account-id",
    "zoom-client-id",
    "zoom-client-secret",
    "zoom-meeting-id",
)

ROLE_OWNER = "owner"
ROLE_MANAGER = "manager"

def _snap_value(snap: dict[str, str | None], name: str) -> str | None:
    value = (snap.get(name) or "").strip()
    if not value or value in {"REPLACE_ME", "MOVED_TO_HASH"}:
        return None
    return value

def index_secret_snapshot(*, owner: bool) -> dict[str, str | None]:
    names = INDEX_SECRET_NAMES if owner else MANAGER_SECRET_NAMES
    return keyvault.get_secrets(kv_name(), names)

def _pin_material(env_hash: str, env_plain: str, secret_hash: str, secret_plain: str) -> str | None:
    if os.environ.get(env_hash):
        return os.environ[env_hash]
    try:
        return get_secret(secret_hash)
    except Exception:  # noqa: BLE001
        plain = os.environ.get(env_plain)
        if plain:
            return plain
        try:
            value = get_secret(secret_plain)
        except Exception:  # noqa: BLE001
            return None
        if not value or value == "MOVED_TO_HASH":
            return None
        return value

def manager_pin_material() -> str | None:
    """Existing team PIN — becomes the manager account once an owner PIN exists."""
    return _pin_material("UI_PIN_HASH", "UI_PIN", "ui-pin-hash", "ui-pin")

def owner_pin_material() -> str | None:
    return _pin_material(
        "UI_OWNER_PIN_HASH", "UI_OWNER_PIN", "ui-owner-pin-hash", "ui-owner-pin"
    )

def owner_pin_configured() -> bool:
    return bool(owner_pin_material())

def resolve_pin_role(candidate: str) -> str | None:
    """Return owner/manager role for a PIN, or None if it does not match."""
    owner_mat = owner_pin_material()
    if owner_mat and verify_pin(candidate, owner_mat):
        return ROLE_OWNER

    manager_mat = manager_pin_material()
    if not manager_mat or not verify_pin(candidate, manager_mat):
        return None

    # Until an owner PIN is created, the existing PIN keeps full access so the
    # operator is never locked out of section 4. After owner PIN exists, this
    # same PIN is manager-only.
    if owner_mat:
        return ROLE_MANAGER
    return ROLE_OWNER

def session_role() -> str:
    role = session.get("role") or ROLE_MANAGER
    return role if role in {ROLE_OWNER, ROLE_MANAGER} else ROLE_MANAGER

def is_owner() -> bool:
    return session.get("authed") and session_role() == ROLE_OWNER

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped

def owner_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login"))
        if not is_owner():
            flash("Only the owner can change those settings.", "error")
            return redirect(url_for("index"))
        return view(*args, **kwargs)

    return wrapped

def public_base() -> str:
    return platforms.public_base_url(public_host())

def rtmp_host() -> str:
    """Hostname Zoom should publish to. Prefer the FQDN over a bare IP."""
    return public_base().split("://", 1)[-1].rstrip("/")

def ingest_target() -> tuple[str, str, str]:
    """(stream_url, stream_key, page_url) for Zoom's custom livestream."""
    return (
        f"rtmp://{rtmp_host()}/live",
        get_secret("ingest-stream-key"),
        public_base() + "/",
    )

def sync_secrets() -> None:
    subprocess.run(["/opt/multistream/bin/sync-secrets.sh"], check=True)

def hash_pin(pin: str, *, salt: bytes | None = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        pin.strip().encode("utf-8"),
        salt,
        HASH_ITERATIONS,
    )
    return f"{HASH_PREFIX}${HASH_ITERATIONS}${salt.hex()}${digest.hex()}"

def verify_pin(candidate: str, stored: str) -> bool:
    stored = stored.strip()
    candidate = candidate.strip()
    if stored.startswith(f"{HASH_PREFIX}$"):
        try:
            _, iter_s, salt_hex, digest_hex = stored.split("$", 3)
            iterations = int(iter_s)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(digest_hex)
        except (ValueError, TypeError):
            return False
        got = hashlib.pbkdf2_hmac(
            "sha256",
            candidate.encode("utf-8"),
            salt,
            iterations,
        )
        return hmac.compare_digest(got, expected)
    # Legacy plaintext (migrate on successful login / harden script)
    return hmac.compare_digest(candidate, stored)

def public_host() -> str:
    if PUBLIC_HOST:
        return PUBLIC_HOST
    return load_config().get("PUBLIC_HOST", request.host.split(":")[0])

def mask(value: str) -> str:
    if not value or value == "REPLACE_ME":
        return "(not set)"
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:4]}••••{value[-4:]}"

def humanize_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"

def humanize_duration(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"

def local_time(iso_value: str) -> str:
    try:
        parsed = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    except ValueError:
        return iso_value
    return parsed.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")

def _lock_state() -> dict:
    if not LOGIN_LOCK_FILE.exists():
        return {"by_ip": {}}
    try:
        return json.loads(LOGIN_LOCK_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"by_ip": {}}

def _save_lock_state(state: dict) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LOGIN_LOCK_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(LOGIN_LOCK_FILE)
    try:
        LOGIN_LOCK_FILE.chmod(0o600)
    except OSError:
        pass

def login_lock_info(ip: str) -> tuple[bool, str]:
    """Return (locked, human message)."""
    state = _lock_state()
    entry = (state.get("by_ip") or {}).get(ip) or {}
    until_raw = entry.get("locked_until")
    if not until_raw:
        return False, ""
    try:
        until = datetime.fromisoformat(until_raw.replace("Z", "+00:00"))
    except ValueError:
        return False, ""
    now = datetime.now(timezone.utc)
    if until <= now:
        return False, ""
    local = until.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
    return True, f"Too many incorrect PIN attempts. Login locked until {local}."

def record_login_failure(ip: str) -> tuple[bool, str]:
    """Track a failed PIN. Returns (just_locked, message)."""
    state = _lock_state()
    by_ip = state.setdefault("by_ip", {})
    entry = by_ip.setdefault(ip, {"fails": 0, "locked_until": None, "alert_sent": False})

    # Clear an expired lock so the next window starts fresh.
    until_raw = entry.get("locked_until")
    if until_raw:
        try:
            until = datetime.fromisoformat(until_raw.replace("Z", "+00:00"))
            if until <= datetime.now(timezone.utc):
                entry["fails"] = 0
                entry["locked_until"] = None
                entry["alert_sent"] = False
        except ValueError:
            entry["locked_until"] = None

    entry["fails"] = int(entry.get("fails") or 0) + 1
    just_locked = False
    if entry["fails"] >= LOGIN_FAIL_LIMIT:
        until = datetime.now(timezone.utc) + timedelta(minutes=LOGIN_LOCK_MINUTES)
        entry["locked_until"] = until.strftime("%Y-%m-%dT%H:%M:%SZ")
        just_locked = True
        if not entry.get("alert_sent"):
            when = until.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
            notify.send_alert(
                get_secret_optional,
                subject="ISKCON Deoghar Multistreaming: login locked after failed PIN attempts",
                body=(
                    f"ISKCON Deoghar Multistreaming received {LOGIN_FAIL_LIMIT} incorrect PIN entries "
                    f"from {ip}.\n\n"
                    f"Login is locked for {LOGIN_LOCK_MINUTES} minutes "
                    f"(until {when}).\n\n"
                    "If this was not you, rotate the UI PIN from a trusted network "
                    "after the lock expires."
                ),
            )
            entry["alert_sent"] = True

    by_ip[ip] = entry
    _save_lock_state(state)
    if just_locked:
        locked, msg = login_lock_info(ip)
        return locked, msg
    remaining = LOGIN_FAIL_LIMIT - int(entry["fails"])
    return False, f"Incorrect PIN. {remaining} attempt(s) left before a 60-minute lock."

def clear_login_failures(ip: str) -> None:
    state = _lock_state()
    by_ip = state.get("by_ip") or {}
    if ip in by_ip:
        by_ip.pop(ip, None)
        _save_lock_state(state)

def read_history(limit: int = 25) -> list[dict]:
    """Group per-destination records into sessions, newest first."""
    if not HISTORY_FILE.exists():
        return []

    sessions: dict[str, dict] = {}
    for line in HISTORY_FILE.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        session_id = record.get("session", "unknown")
        session = sessions.setdefault(
            session_id,
            {"session": session_id, "started": record.get("started", ""), "destinations": []},
        )
        started = record.get("started", "")
        if started and (not session["started"] or started < session["started"]):
            session["started"] = started

        status = record.get("status", "unknown")
        session["destinations"].append(
            {
                "name": record.get("destination", "?"),
                "status": status,
                "status_label": STATUS_LABELS.get(status, status.replace("_", " ").title()),
                "ok": status == "delivered",
                "duration": humanize_duration(record.get("duration_sec", 0)),
                "size": humanize_bytes(record.get("bytes", 0)),
                "errors": record.get("errors", []),
            }
        )

    ordered = sorted(sessions.values(), key=lambda item: item["started"], reverse=True)
    for session in ordered:
        session["when"] = local_time(session["started"])
        session["destinations"].sort(key=lambda item: item["name"])
        session["all_ok"] = all(dest["ok"] for dest in session["destinations"])
    return ordered[:limit]

def read_enabled() -> dict[str, bool]:
    defaults = {"youtube": True, "facebook": True, "enhance": False}
    if not ENABLED_FILE.exists():
        return defaults
    values = defaults.copy()
    for line in ENABLED_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip().upper()
        on = raw.strip() == "1"
        if key == "YOUTUBE_ENABLED":
            values["youtube"] = on
        elif key == "FACEBOOK_ENABLED":
            values["facebook"] = on
        elif key == "ENHANCE_RELAY":
            values["enhance"] = on
    return values

def write_enabled(
    youtube: bool,
    facebook: bool,
    *,
    enhance: bool | None = None,
) -> None:
    current = read_enabled()
    if enhance is None:
        enhance = current["enhance"]
    ENABLED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENABLED_FILE.write_text(
        f"YOUTUBE_ENABLED={'1' if youtube else '0'}\n"
        f"FACEBOOK_ENABLED={'1' if facebook else '0'}\n"
        f"ENHANCE_RELAY={'1' if enhance else '0'}\n"
    )
    os.chmod(ENABLED_FILE, 0o644)

def destination_running(name: str) -> bool:
    pid_path = RUN_DIR / f"{name}.pid"
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True

def apply_destinations() -> str:
    result = subprocess.run(
        [str(APPLY_SCRIPT), "apply"],
        check=False,
        capture_output=True,
        text=True,
    )
    return (result.stdout or "") + (result.stderr or "")

def live_status() -> dict:
    """Ask MediaMTX whether Zoom is publishing right now."""
    enabled = read_enabled()
    status = {
        "publishing": False,
        "path": "",
        "readers": 0,
        "since": "",
        "error": "",
        "youtube_enabled": enabled["youtube"],
        "facebook_enabled": enabled["facebook"],
        "youtube_running": destination_running("youtube"),
        "facebook_running": destination_running("facebook"),
        "enhance_enabled": enabled["enhance"],
    }

    try:
        with urllib.request.urlopen(MEDIAMTX_API, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        status["error"] = f"Relay API unreachable: {exc}"
        return status

    for item in payload.get("items", []):
        if not item.get("ready"):
            continue
        status["publishing"] = True
        status["path"] = item.get("name", "")
        status["readers"] = len(item.get("readers", []))
        ready_time = item.get("readyTime") or ""
        if ready_time:
            status["since"] = local_time(ready_time[:19] + "Z")
        break

    if status["publishing"]:
        status["destinations_connected"] = status["readers"]
    return status

@app.context_processor
def inject_csrf() -> dict[str, str]:
    return {"csrf_token": generate_csrf}

@app.get("/login")
def login():
    if session.get("authed"):
        return redirect(url_for("index"))
    locked, message = login_lock_info(get_remote_address())
    return render_template("login.html", login_locked=locked, lock_message=message)

@app.post("/login")
@limiter.limit("20 per hour")
def login_post():
    ip = get_remote_address()
    locked, message = login_lock_info(ip)
    if locked:
        flash(message, "error")
        return render_template("login.html", login_locked=True, lock_message=message), 429

    pin = request.form.get("pin", "")
    try:
        role = resolve_pin_role(pin)
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not verify PIN: {exc}", "error")
        return render_template("login.html", login_locked=False, lock_message=""), 500
    if not role:
        just_locked, fail_msg = record_login_failure(ip)
        flash(fail_msg, "error")
        return (
            render_template(
                "login.html",
                login_locked=just_locked,
                lock_message=fail_msg if just_locked else "",
            ),
            429 if just_locked else 401,
        )
    clear_login_failures(ip)
    session.clear()
    session["authed"] = True
    session["role"] = role
    session.permanent = True
    return redirect(url_for("index"))

@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.get("/")
@login_required
def index():
    owner = is_owner()
    snap = index_secret_snapshot(owner=owner)
    ingest = _snap_value(snap, "ingest-stream-key") or ""
    yt = _snap_value(snap, "youtube-stream-key") or "REPLACE_ME"
    fb = _snap_value(snap, "facebook-stream-key") or "REPLACE_ME"
    if owner and not ingest and yt == "REPLACE_ME" and fb == "REPLACE_ME":
        # Distinguish total KV failure from empty vault — retry once for the
        # three critical keys so the flash still surfaces a real outage.
        try:
            ingest = get_secret("ingest-stream-key")
            yt = get_secret("youtube-stream-key")
            fb = get_secret("facebook-stream-key")
        except Exception as exc:  # noqa: BLE001
            flash(f"Key Vault read failed: {exc}", "error")
            ingest, yt, fb = "", "REPLACE_ME", "REPLACE_ME"

    host = public_host()
    zoom_server = f"rtmp://{host}/live"
    enabled = read_enabled()

    google_client_id = _snap_value(snap, "google-oauth-client-id") or ""
    google_client_secret = _snap_value(snap, "google-oauth-client-secret") or ""
    facebook_app_id = _snap_value(snap, "facebook-app-id") or ""
    facebook_app_secret = _snap_value(snap, "facebook-app-secret") or ""
    facebook_config_id = _snap_value(snap, "facebook-login-config-id") or ""
    zoom_account_id = _snap_value(snap, "zoom-account-id") or ""
    zoom_client_id = _snap_value(snap, "zoom-client-id") or ""
    zoom_client_secret = _snap_value(snap, "zoom-client-secret") or ""
    zoom_meeting_id = _snap_value(snap, "zoom-meeting-id") or ""
    smtp_user = _snap_value(snap, "smtp-user") or ""
    smtp_password = _snap_value(snap, "smtp-password") or ""

    oauth = {
        "google_app": bool(google_client_id and google_client_secret),
        "facebook_app": bool(facebook_app_id and facebook_app_secret),
        "youtube_connected": bool(_snap_value(snap, "youtube-oauth-tokens")),
        "facebook_connected": bool(_snap_value(snap, "facebook-page-token")),
    }
    default_title = (
        _snap_value(snap, "default-stream-title") or platforms.DEFAULT_LIVE_TITLE
    )
    default_description = (
        _snap_value(snap, "default-stream-description")
        or platforms.DEFAULT_LIVE_DESCRIPTION
    )
    title = _snap_value(snap, "stream-title") or default_title
    description = _snap_value(snap, "stream-description") or default_description
    yt_watch = _snap_value(snap, "youtube-watch-url") or ""
    fb_watch = _snap_value(snap, "facebook-watch-url") or ""
    fb_page = _snap_value(snap, "facebook-page-name") or ""

    return render_template(
        "index.html",
        is_owner=owner,
        role=session_role(),
        owner_pin_configured=owner_pin_configured(),
        zoom_api_ready=bool(zoom_account_id and zoom_client_id and zoom_client_secret),
        zoom_meeting_id=zoom_meeting_id,
        zoom_account_id_set=bool(zoom_account_id),
        zoom_account_id_masked=mask(zoom_account_id),
        zoom_client_id_set=bool(zoom_client_id),
        zoom_client_id_masked=mask(zoom_client_id),
        zoom_client_secret_set=bool(zoom_client_secret),
        zoom_client_secret_masked=mask(zoom_client_secret),
        smtp_user_set=bool(smtp_user),
        smtp_user_value=smtp_user,
        smtp_password_set=bool(smtp_password),
        smtp_password_masked=mask(smtp_password),
        alert_recipient=notify.ALERT_TO,
        zoom_server=zoom_server,
        zoom_key=ingest,
        youtube_masked=mask(yt),
        facebook_masked=mask(fb),
        youtube_set=yt not in ("", "REPLACE_ME"),
        facebook_set=fb not in ("", "REPLACE_ME"),
        youtube_enabled=enabled["youtube"],
        facebook_enabled=enabled["facebook"],
        enhance_enabled=enabled["enhance"],
        live=live_status(),
        recent=read_history(limit=3),
        oauth=oauth,
        stream_title=title,
        stream_description=description,
        default_live_title=default_title,
        default_live_description=default_description,
        youtube_watch_url=yt_watch,
        facebook_watch_url=fb_watch,
        facebook_page_name=fb_page,
        oauth_redirect_base=public_base(),
        google_client_id_set=bool(google_client_id),
        google_client_id_masked=mask(google_client_id),
        google_client_secret_set=bool(google_client_secret),
        google_client_secret_masked=mask(google_client_secret),
        facebook_app_id_set=bool(facebook_app_id),
        facebook_app_id_masked=mask(facebook_app_id),
        facebook_app_secret_set=bool(facebook_app_secret),
        facebook_app_secret_masked=mask(facebook_app_secret),
        facebook_config_id_set=bool(facebook_config_id),
        facebook_config_id_masked=mask(facebook_config_id),
    )

@app.get("/history")
@login_required
def history():
    return render_template("history.html", sessions=read_history(limit=25))

@app.get("/api/status")
@login_required
@limiter.exempt
def api_status():
    return live_status()


@app.get("/api/meeting-status")
@login_required
@limiter.exempt
def api_meeting_status():
    """Report whether a Zoom meeting is currently in progress."""
    if not zoom.configured(get_secret):
        return {
            "state": "unconfigured",
            "meeting_id": "",
            "message": "Zoom API is not configured.",
        }

    raw = (request.args.get("meeting_id") or "").strip()
    if not raw:
        raw = get_secret_optional("zoom-meeting-id") or ""
    if not raw.strip():
        return {
            "state": "missing",
            "meeting_id": "",
            "message": "No meeting ID entered.",
        }

    try:
        meeting_id = zoom.normalize_meeting_id(raw)
        detail = zoom.get_meeting(get_secret, meeting_id)
    except zoom.ZoomError as exc:
        return {
            "state": "error",
            "meeting_id": raw.strip(),
            "message": str(exc),
        }

    status = str(detail.get("status") or "").lower()
    topic = str(detail.get("topic") or "").strip()
    in_progress = status == "started"
    message = (
        f"Meeting in progress{f' — {topic}' if topic else ''}."
        if in_progress
        else f"Meeting not in progress{f' — {topic}' if topic else ''}."
    )
    return {
        "state": "in_progress" if in_progress else "not_started",
        "meeting_id": meeting_id,
        "topic": topic,
        "zoom_status": status,
        "message": message,
    }


@app.post("/destinations")
@login_required
def update_destinations():
    youtube = request.form.get("youtube") == "1"
    facebook = request.form.get("facebook") == "1"
    # Managers cannot toggle enhance; always keep direct copy.
    enhance = is_owner() and request.form.get("enhance") == "1"
    if not youtube and not facebook:
        flash("Keep at least one destination on.", "error")
        return redirect(url_for("index"))
    try:
        write_enabled(youtube, facebook, enhance=enhance)
        output = apply_destinations()
        parts = []
        if youtube:
            parts.append("YouTube")
        if facebook:
            parts.append("Facebook")
        mode = "enhanced processing" if enhance else "direct copy"
        flash(
            f"Streaming to: {', '.join(parts)} ({mode}). "
            + (
                "Applied to the live session."
                if "started" in output
                or "stopped" in output
                or "already" in output
                or "restarted" in output
                else "Saved for the next stream."
            ),
            "ok",
        )
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not update destinations: {exc}", "error")
    return redirect(url_for("index"))

def _prepare_lives(title: str, description: str, enabled: dict[str, bool]) -> tuple[list[str], list[str]]:
    """Create YT/FB lives for enabled destinations that need one."""
    status = platforms.prepare_readiness(
        get_secret,
        set_secret,
        youtube_enabled=enabled["youtube"],
        facebook_enabled=enabled["facebook"],
        verify_apis=True,
    )

    jobs = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        if enabled["youtube"] and not status.youtube.ready:
            jobs["YouTube"] = pool.submit(
                platforms.youtube_prepare_live, get_secret, set_secret, title, description
            )
        if enabled["facebook"] and not status.facebook.ready:
            jobs["Facebook"] = pool.submit(
                platforms.facebook_prepare_live, get_secret, set_secret, title, description
            )

    ready: list[str] = []
    failures: list[str] = []
    for label, job in jobs.items():
        try:
            job.result()
            ready.append(label)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{label}: {exc}")

    # Destinations that were already live keep their existing objects.
    if enabled["youtube"] and status.youtube.ready and "YouTube" not in ready:
        ready.append("YouTube")
    if enabled["facebook"] and status.facebook.ready and "Facebook" not in ready:
        ready.append("Facebook")
    return ready, failures

@app.post("/go-live")
@login_required
@limiter.limit("20 per hour")
def go_live():
    """One action: pick destinations, set title, create lives, tell Zoom to stream."""
    youtube = request.form.get("youtube") == "1"
    facebook = request.form.get("facebook") == "1"
    # Managers cannot enable enhance; force direct copy on this B2s VM.
    enhance = is_owner() and request.form.get("enhance") == "1"
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    meeting_raw = request.form.get("meeting_id", "").strip()

    if not youtube and not facebook:
        flash("Choose at least one destination.", "error")
        return redirect(url_for("index"))
    if not title:
        flash("Enter a title for the stream.", "error")
        return redirect(url_for("index"))

    try:
        meeting_id = zoom.normalize_meeting_id(meeting_raw)
    except zoom.ZoomError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))

    enabled = {"youtube": youtube, "facebook": facebook}
    try:
        write_enabled(youtube, facebook, enhance=enhance)
        # Same text serves this stream and any later auto-prepare.
        set_secrets(
            {
                "stream-title": title,
                "stream-description": description or title,
                "default-stream-title": title,
                "default-stream-description": description or title,
                "zoom-meeting-id": meeting_id,
            }
        )
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not save settings: {exc}", "error")
        return redirect(url_for("index"))

    ready, failures = _prepare_lives(title, description or title, enabled)
    for failure in failures:
        flash(failure, "error")
    if not ready:
        flash("No destination could be prepared, so Zoom was not started.", "error")
        return redirect(url_for("index"))

    try:
        sync_secrets()
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not refresh relay keys: {exc}", "error")
        return redirect(url_for("index"))

    try:
        stream_url, stream_key, page_url = ingest_target()
        zoom.configure_livestream(
            get_secret,
            meeting_id,
            stream_url=stream_url,
            stream_key=stream_key,
            page_url=page_url,
        )
        zoom.start_livestream(get_secret, meeting_id, display_name=title)
    except zoom.ZoomError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not start the Zoom livestream: {exc}", "error")
        return redirect(url_for("index"))

    apply_destinations()
    flash(
        f"Streaming to {' and '.join(ready)}. Zoom is publishing to MultiStream — "
        "give it a few seconds to appear on each platform.",
        "ok",
    )
    return redirect(url_for("index"))

@app.post("/stop-live")
@login_required
@limiter.limit("30 per hour")
def stop_live():
    meeting_raw = request.form.get("meeting_id", "").strip()
    try:
        meeting_id = zoom.normalize_meeting_id(
            meeting_raw or (get_secret_optional("zoom-meeting-id") or "")
        )
    except zoom.ZoomError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))

    try:
        zoom.stop_livestream(get_secret, meeting_id)
        flash(
            "Zoom livestream stopped. The meeting itself is still running.",
            "ok",
        )
    except zoom.ZoomError as exc:
        flash(str(exc), "error")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not stop the Zoom livestream: {exc}", "error")
    return redirect(url_for("index"))

@app.post("/keys")
@owner_required
def update_keys():
    yt = request.form.get("youtube_stream_key", "").strip()
    fb = request.form.get("facebook_stream_key", "").strip()

    try:
        batch: dict[str, str] = {}
        if yt:
            batch["youtube-stream-key"] = yt
        if fb:
            batch["facebook-stream-key"] = fb
        if batch:
            set_secrets(batch)
        sync_secrets()
        flash("Saved. New streams will use the updated keys.", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"Update failed: {exc}", "error")
    return redirect(url_for("index"))

@app.post("/pins")
@owner_required
def update_pins():
    """Owner can set the owner PIN and/or rotate the manager PIN."""
    owner_pin = request.form.get("owner_pin", "").strip()
    manager_pin = request.form.get("manager_pin", "").strip()
    if not owner_pin and not manager_pin:
        flash("Enter a new owner PIN and/or manager PIN.", "error")
        return redirect(url_for("index"))

    def _valid(pin: str) -> bool:
        return pin.isdigit() and 4 <= len(pin) <= 12

    try:
        batch: dict[str, str] = {}
        if owner_pin:
            if not _valid(owner_pin):
                flash("Owner PIN must be 4–12 digits.", "error")
                return redirect(url_for("index"))
            owner_hash = hash_pin(owner_pin)
            batch["ui-owner-pin-hash"] = owner_hash
            batch["ui-owner-pin"] = "MOVED_TO_HASH"
            os.environ["UI_OWNER_PIN_HASH"] = owner_hash
            os.environ.pop("UI_OWNER_PIN", None)
        if manager_pin:
            if not _valid(manager_pin):
                flash("Manager PIN must be 4–12 digits.", "error")
                return redirect(url_for("index"))
            manager_hash = hash_pin(manager_pin)
            batch["ui-pin-hash"] = manager_hash
            batch["ui-pin"] = "MOVED_TO_HASH"
            os.environ["UI_PIN_HASH"] = manager_hash
            os.environ.pop("UI_PIN", None)
        set_secrets(batch)
        keyvault.invalidate("ui-owner-pin-hash", "ui-pin-hash", "ui-owner-pin", "ui-pin")
        if owner_pin and manager_pin:
            flash(
                "Owner and manager PINs updated. Share only the manager PIN with operators.",
                "ok",
            )
        elif owner_pin:
            flash(
                "Owner PIN saved. The existing team PIN is now the manager PIN "
                "(sections 1–3 only).",
                "ok",
            )
        else:
            flash("Manager PIN updated.", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not update PINs: {exc}", "error")
    return redirect(url_for("index"))

@app.post("/metadata")
@login_required
def save_metadata():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    if not title:
        flash("Title is required.", "error")
        return redirect(url_for("index"))
    try:
        set_secrets({"stream-title": title, "stream-description": description})
        flash("Title and description saved.", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not save metadata: {exc}", "error")
    return redirect(url_for("index"))

@app.post("/defaults")
@owner_required
def save_defaults():
    """Defaults used when Zoom goes live without Prepare live."""
    title = request.form.get("default_title", "").strip()
    description = request.form.get("default_description", "").strip()
    if not title:
        flash("Default title is required.", "error")
        return redirect(url_for("index"))
    try:
        set_secrets(
            {
                "default-stream-title": title,
                "default-stream-description": description or title,
            }
        )
        flash("Default title and description saved for auto-prepare.", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not save defaults: {exc}", "error")
    return redirect(url_for("index"))

@app.post("/prepare-live")
@owner_required
@limiter.limit("10 per hour")
def prepare_live():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    if not title:
        flash("Enter a title before preparing the live.", "error")
        return redirect(url_for("index"))

    enabled = read_enabled()
    if not enabled["youtube"] and not enabled["facebook"]:
        flash("Turn on at least one destination first.", "error")
        return redirect(url_for("index"))

    try:
        set_secrets({"stream-title": title, "stream-description": description})

        # YouTube and Facebook are independent round trips; run them together
        # so the operator waits for the slower one rather than their sum.
        jobs = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            if enabled["youtube"]:
                jobs["YouTube"] = pool.submit(
                    platforms.youtube_prepare_live, get_secret, set_secret, title, description
                )
            if enabled["facebook"]:
                jobs["Facebook"] = pool.submit(
                    platforms.facebook_prepare_live, get_secret, set_secret, title, description
                )

        messages: list[str] = []
        failures: list[str] = []
        for label, job in jobs.items():
            try:
                messages.append(f"{label} ready — {job.result().watch_url}")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{label}: {exc}")

        if messages:
            sync_secrets()
            apply_destinations()
            flash(" ".join(messages) + " Now start Custom Live Streaming in Zoom.", "ok")
        for failure in failures:
            flash(failure, "error")
    except Exception as exc:  # noqa: BLE001
        flash(f"Prepare live failed: {exc}", "error")
    return redirect(url_for("index"))

@app.post("/metadata/push")
@login_required
@limiter.limit("30 per hour")
def push_metadata():
    """Retitle the current lives in place — safe while Zoom is publishing."""
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    if not title:
        flash("Title is required.", "error")
        return redirect(url_for("index"))

    enabled = read_enabled()
    try:
        set_secrets({"stream-title": title, "stream-description": description})
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not save metadata: {exc}", "error")
        return redirect(url_for("index"))

    jobs = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        if enabled["youtube"]:
            jobs["YouTube"] = pool.submit(
                platforms.youtube_update_metadata, get_secret, set_secret, title, description
            )
        if enabled["facebook"]:
            jobs["Facebook"] = pool.submit(
                platforms.facebook_update_metadata, get_secret, title, description
            )

    updated: list[str] = []
    for label, job in jobs.items():
        try:
            job.result()
            updated.append(label)
        except Exception as exc:  # noqa: BLE001
            flash(f"{label}: {exc}", "error")

    if updated:
        flash(
            f"Title and description updated on {' and '.join(updated)}. "
            "Stream keys unchanged — the broadcast keeps running.",
            "ok",
        )
    return redirect(url_for("index"))

@app.post("/oauth/youtube/start")
@owner_required
def oauth_youtube_start():
    try:
        state = platforms.new_oauth_state()
        session["oauth_yt_state"] = state
        return redirect(platforms.youtube_authorize_url(get_secret, public_host(), state))
    except platforms.PlatformError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))

@app.get("/oauth/youtube/callback")
@limiter.exempt
def oauth_youtube_callback():
    if not session.get("authed"):
        return redirect(url_for("login"))
    if not is_owner():
        flash("Only the owner can connect platforms.", "error")
        return redirect(url_for("index"))
    if request.args.get("state") != session.get("oauth_yt_state"):
        flash("YouTube connect failed: invalid OAuth state.", "error")
        return redirect(url_for("index"))
    if request.args.get("error"):
        flash(f"YouTube connect denied: {request.args.get('error')}", "error")
        return redirect(url_for("index"))
    code = request.args.get("code", "")
    try:
        platforms.youtube_exchange_code(get_secret, set_secret, public_host(), code)
        flash("YouTube connected. Title/description will apply on Prepare live.", "ok")
    except platforms.PlatformError as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))

@app.post("/oauth/facebook/start")
@owner_required
def oauth_facebook_start():
    try:
        state = platforms.new_oauth_state()
        session["oauth_fb_state"] = state
        return redirect(platforms.facebook_authorize_url(get_secret, public_host(), state))
    except platforms.PlatformError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))

@app.get("/oauth/facebook/callback")
@limiter.exempt
def oauth_facebook_callback():
    if not session.get("authed"):
        return redirect(url_for("login"))
    if not is_owner():
        flash("Only the owner can connect platforms.", "error")
        return redirect(url_for("index"))
    if request.args.get("state") != session.get("oauth_fb_state"):
        flash("Facebook connect failed: invalid OAuth state.", "error")
        return redirect(url_for("index"))
    if request.args.get("error"):
        flash(f"Facebook connect denied: {request.args.get('error_description') or request.args.get('error')}", "error")
        return redirect(url_for("index"))
    code = request.args.get("code", "")
    try:
        page_name = platforms.facebook_exchange_code(get_secret, set_secret, public_host(), code)
        flash(f"Facebook connected to Page “{page_name}”.", "ok")
    except platforms.PlatformError as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))

@app.post("/oauth/credentials")
@owner_required
def save_oauth_credentials():
    """Store Google/Meta/Zoom app credentials (one-time admin setup)."""
    fields = {
        "google-oauth-client-id": request.form.get("google_client_id", "").strip(),
        "google-oauth-client-secret": request.form.get("google_client_secret", "").strip(),
        "facebook-app-id": request.form.get("facebook_app_id", "").strip(),
        "facebook-app-secret": request.form.get("facebook_app_secret", "").strip(),
        "facebook-login-config-id": request.form.get("facebook_config_id", "").strip(),
        "zoom-account-id": request.form.get("zoom_account_id", "").strip(),
        "zoom-client-id": request.form.get("zoom_client_id", "").strip(),
        "zoom-client-secret": request.form.get("zoom_client_secret", "").strip(),
    }
    try:
        to_save = {name: value for name, value in fields.items() if value}
        if not to_save:
            flash("Enter at least one credential value.", "error")
        else:
            set_secrets(to_save)
            flash("OAuth app credentials saved to Key Vault.", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not save credentials: {exc}", "error")
    return redirect(url_for("index"))

@app.post("/email/credentials")
@owner_required
def save_email_credentials():
    """Store the Gmail account and App Password used for alert emails."""
    user = request.form.get("smtp_user", "").strip()
    # Google shows App Passwords in groups of four; the spaces are display only.
    password = request.form.get("smtp_password", "").replace(" ", "").strip()

    to_save: dict[str, str] = {}
    if user:
        to_save["smtp-user"] = user
    if password:
        to_save["smtp-password"] = password
    if not to_save:
        flash("Enter a Gmail address and/or an App Password.", "error")
        return redirect(url_for("index"))

    if password and len(password) != 16:
        flash(
            f"That App Password is {len(password)} characters; Google's are 16 letters. "
            "Paste the one from Google Account \u203a Security \u203a App passwords, "
            "not your Gmail password.",
            "error",
        )
        return redirect(url_for("index"))

    try:
        set_secrets(to_save)
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not save email settings: {exc}", "error")
        return redirect(url_for("index"))

    ok, detail = notify.check_credentials(get_secret_optional)
    if ok:
        flash("Email settings saved and Gmail accepted them.", "ok")
    else:
        flash(f"Saved, but Gmail rejected them: {detail}", "error")
    return redirect(url_for("index"))

@app.post("/email/test")
@owner_required
def send_test_email():
    """Prove alerts work end to end, so a broken password is obvious now."""
    sent = notify.send_alert(
        get_secret_optional,
        subject="ISKCON stream: test email",
        body=(
            "This is a test from the owner console.\n\n"
            "If you are reading it, streaming and login alerts will reach you."
        ),
    )
    if sent:
        flash(f"Test email sent to {notify.ALERT_TO}.", "ok")
    else:
        ok, detail = notify.check_credentials(get_secret_optional)
        flash(f"Test email failed: {detail}" if not ok else "Test email failed. See logs.", "error")
    return redirect(url_for("index"))

@app.get("/healthz")
@limiter.exempt
def healthz():
    revision = ""
    deployed_at = ""
    rev_path = Path("/opt/multistream/etc/deploy-revision")
    at_path = Path("/opt/multistream/etc/deployed-at")
    if rev_path.exists():
        revision = rev_path.read_text().strip()
    if at_path.exists():
        deployed_at = at_path.read_text().strip()
    return {"ok": True, "revision": revision, "deployed_at": deployed_at}

@app.errorhandler(429)
def ratelimit_handler(_exc):
    flash("Too many login attempts. Try again in 15 minutes.", "error")
    return render_template("login.html"), 429

def main() -> None:
    app.run(host="127.0.0.1", port=8080)

if __name__ == "__main__":
    main()
