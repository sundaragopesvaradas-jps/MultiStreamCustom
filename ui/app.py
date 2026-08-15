#!/usr/bin/env python3
"""PIN-protected UI to update YouTube/Facebook stream keys in Azure Key Vault."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

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
import platforms

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


def get_secret_optional(name: str) -> str | None:
    return platforms.get_optional(get_secret, name)


def public_base() -> str:
    return platforms.public_base_url(public_host())


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


def current_pin_material() -> str:
    if os.environ.get("UI_PIN_HASH"):
        return os.environ["UI_PIN_HASH"]
    try:
        return get_secret("ui-pin-hash")
    except Exception:  # noqa: BLE001
        return os.environ.get("UI_PIN") or get_secret("ui-pin")


def pin_ok(candidate: str) -> bool:
    return verify_pin(candidate, current_pin_material())


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


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
    return parsed.astimezone().strftime("%d %b %Y, %I:%M %p")


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
    defaults = {"youtube": True, "facebook": True}
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
    return values


def write_enabled(youtube: bool, facebook: bool) -> None:
    ENABLED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENABLED_FILE.write_text(
        f"YOUTUBE_ENABLED={'1' if youtube else '0'}\n"
        f"FACEBOOK_ENABLED={'1' if facebook else '0'}\n"
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
    return render_template("login.html")


@app.post("/login")
@limiter.limit("5 per 15 minutes")
def login_post():
    pin = request.form.get("pin", "")
    try:
        ok = pin_ok(pin)
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not verify PIN: {exc}", "error")
        return render_template("login.html"), 500
    if not ok:
        flash("Incorrect PIN.", "error")
        return render_template("login.html"), 401
    session.clear()
    session["authed"] = True
    session.permanent = True
    return redirect(url_for("index"))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def index():
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
    oauth = platforms.oauth_configured(get_secret)
    title = get_secret_optional("stream-title") or ""
    description = get_secret_optional("stream-description") or ""
    yt_watch = get_secret_optional("youtube-watch-url") or ""
    fb_watch = get_secret_optional("facebook-watch-url") or ""
    fb_page = get_secret_optional("facebook-page-name") or ""

    google_client_id = get_secret_optional("google-oauth-client-id") or ""
    google_client_secret = get_secret_optional("google-oauth-client-secret") or ""
    facebook_app_id = get_secret_optional("facebook-app-id") or ""
    facebook_app_secret = get_secret_optional("facebook-app-secret") or ""
    facebook_config_id = get_secret_optional("facebook-login-config-id") or ""

    return render_template(
        "index.html",
        zoom_server=zoom_server,
        zoom_key=ingest,
        youtube_masked=mask(yt),
        facebook_masked=mask(fb),
        youtube_set=yt not in ("", "REPLACE_ME"),
        facebook_set=fb not in ("", "REPLACE_ME"),
        youtube_enabled=enabled["youtube"],
        facebook_enabled=enabled["facebook"],
        live=live_status(),
        recent=read_history(limit=3),
        oauth=oauth,
        stream_title=title,
        stream_description=description,
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


@app.post("/destinations")
@login_required
def update_destinations():
    youtube = request.form.get("youtube") == "1"
    facebook = request.form.get("facebook") == "1"
    if not youtube and not facebook:
        flash("Keep at least one destination on.", "error")
        return redirect(url_for("index"))
    try:
        write_enabled(youtube, facebook)
        output = apply_destinations()
        parts = []
        if youtube:
            parts.append("YouTube")
        if facebook:
            parts.append("Facebook")
        flash(
            f"Streaming to: {', '.join(parts)}. "
            + ("Applied to the live session." if "started" in output or "stopped" in output or "already" in output else "Saved for the next stream."),
            "ok",
        )
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not update destinations: {exc}", "error")
    return redirect(url_for("index"))


@app.post("/keys")
@login_required
def update_keys():
    yt = request.form.get("youtube_stream_key", "").strip()
    fb = request.form.get("facebook_stream_key", "").strip()
    new_pin = request.form.get("new_pin", "").strip()

    try:
        if yt:
            set_secret("youtube-stream-key", yt)
        if fb:
            set_secret("facebook-stream-key", fb)
        if new_pin:
            if not new_pin.isdigit() or not (4 <= len(new_pin) <= 12):
                flash("PIN must be 4–12 digits.", "error")
                return redirect(url_for("index"))
            pin_hash = hash_pin(new_pin)
            set_secret("ui-pin-hash", pin_hash)
            # Remove plaintext PIN if present
            try:
                set_secret("ui-pin", "MOVED_TO_HASH")
            except Exception:  # noqa: BLE001
                pass
            os.environ["UI_PIN_HASH"] = pin_hash
            os.environ.pop("UI_PIN", None)
        sync_secrets()
        flash("Saved. New streams will use the updated keys.", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"Update failed: {exc}", "error")
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
        set_secret("stream-title", title)
        set_secret("stream-description", description)
        flash("Title and description saved.", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not save metadata: {exc}", "error")
    return redirect(url_for("index"))


@app.post("/prepare-live")
@login_required
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
        set_secret("stream-title", title)
        set_secret("stream-description", description)

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
        set_secret("stream-title", title)
        set_secret("stream-description", description)
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
@login_required
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
@login_required
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
@login_required
def save_oauth_credentials():
    """Store Google/Meta app credentials (one-time admin setup)."""
    fields = {
        "google-oauth-client-id": request.form.get("google_client_id", "").strip(),
        "google-oauth-client-secret": request.form.get("google_client_secret", "").strip(),
        "facebook-app-id": request.form.get("facebook_app_id", "").strip(),
        "facebook-app-secret": request.form.get("facebook_app_secret", "").strip(),
        "facebook-login-config-id": request.form.get("facebook_config_id", "").strip(),
    }
    try:
        saved = []
        for name, value in fields.items():
            if value:
                set_secret(name, value)
                saved.append(name)
        if not saved:
            flash("Enter at least one credential value.", "error")
        else:
            flash("OAuth app credentials saved to Key Vault.", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not save credentials: {exc}", "error")
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
