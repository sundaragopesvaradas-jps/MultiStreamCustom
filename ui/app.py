#!/usr/bin/env python3
"""PIN-protected UI to update YouTube/Facebook stream keys in Azure Key Vault."""

from __future__ import annotations

import hmac
import os
import secrets
import subprocess
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

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

CONFIG_ENV = Path("/opt/multistream/etc/multistream.env")
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")


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


def run_az(*args: str) -> str:
    result = subprocess.run(
        ["az", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def get_secret(name: str) -> str:
    return run_az(
        "keyvault",
        "secret",
        "show",
        "--vault-name",
        kv_name(),
        "--name",
        name,
        "--query",
        "value",
        "-o",
        "tsv",
    )


def set_secret(name: str, value: str) -> None:
    subprocess.run(
        [
            "az",
            "keyvault",
            "secret",
            "set",
            "--vault-name",
            kv_name(),
            "--name",
            name,
            "--value",
            value,
            "--output",
            "none",
        ],
        check=True,
    )


def sync_secrets() -> None:
    subprocess.run(["/opt/multistream/bin/sync-secrets.sh"], check=True)


def pin_ok(candidate: str) -> bool:
    expected = os.environ.get("UI_PIN") or get_secret("ui-pin")
    return hmac.compare_digest(candidate.strip(), expected.strip())


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


@app.get("/login")
def login():
    if session.get("authed"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.post("/login")
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
    return render_template(
        "index.html",
        zoom_server=zoom_server,
        zoom_key=ingest,
        youtube_masked=mask(yt),
        facebook_masked=mask(fb),
        youtube_set=yt not in ("", "REPLACE_ME"),
        facebook_set=fb not in ("", "REPLACE_ME"),
    )


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
            set_secret("ui-pin", new_pin)
            os.environ["UI_PIN"] = new_pin
        sync_secrets()
        flash("Saved. New streams will use the updated keys.", "ok")
    except Exception as exc:  # noqa: BLE001
        flash(f"Update failed: {exc}", "error")
    return redirect(url_for("index"))


@app.get("/healthz")
def healthz():
    return {"ok": True}


def main() -> None:
    # Prefer env PIN cached after first Key Vault read at boot via systemd
    app.run(host="127.0.0.1", port=8080)


if __name__ == "__main__":
    main()
