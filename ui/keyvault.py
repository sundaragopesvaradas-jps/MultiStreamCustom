"""Key Vault access over REST using the VM's managed identity.

The Azure CLI costs about one second per secret because of interpreter
startup, and a single page render touches well over a dozen secrets. Talking
to Key Vault directly keeps that closer to 0.1s. The CLI stays as a fallback
so the app still works where IMDS is unavailable (local development).
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

IMDS_URL = (
    "http://169.254.169.254/metadata/identity/oauth2/token"
    "?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net"
)
API_VERSION = "7.4"
READ_TTL = 5.0

_lock = threading.Lock()
_token: dict[str, float | str] = {}
_cache: dict[str, tuple[float, str | None]] = {}
_imds_ok = True


class KeyVaultError(Exception):
    """Key Vault read/write failure."""


def _request(url: str, *, method: str = "GET", headers=None, body=None, timeout=10) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = dict(headers or {})
    if data is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _access_token() -> str:
    with _lock:
        expires = float(_token.get("expires", 0) or 0)
        if _token.get("value") and time.time() < expires - 300:
            return str(_token["value"])
    payload = _request(IMDS_URL, headers={"Metadata": "true"})
    token = payload.get("access_token")
    if not token:
        raise KeyVaultError("Managed identity returned no access token.")
    with _lock:
        _token["value"] = token
        _token["expires"] = float(payload.get("expires_on") or (time.time() + 3000))
    return token


def _vault_url(vault: str, name: str) -> str:
    return f"https://{vault}.vault.azure.net/secrets/{urllib.parse.quote(name)}?api-version={API_VERSION}"


def _az_get(vault: str, name: str) -> str:
    result = subprocess.run(
        [
            "az", "keyvault", "secret", "show",
            "--vault-name", vault, "--name", name,
            "--query", "value", "-o", "tsv",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _az_set(vault: str, name: str, value: str) -> None:
    subprocess.run(
        [
            "az", "keyvault", "secret", "set",
            "--vault-name", vault, "--name", name,
            "--value", value, "--output", "none",
        ],
        check=True,
    )


def get_secret(vault: str, name: str, *, use_cache: bool = True) -> str:
    """Return a secret value. Raises KeyVaultError when it does not exist."""
    global _imds_ok
    key = f"{vault}/{name}"
    now = time.time()
    if use_cache:
        with _lock:
            hit = _cache.get(key)
        if hit and now < hit[0]:
            if hit[1] is None:
                raise KeyVaultError(f"Secret {name} not found.")
            return hit[1]

    value: str | None = None
    if _imds_ok:
        try:
            token = _access_token()
            payload = _request(
                _vault_url(vault, name),
                headers={"Authorization": f"Bearer {token}"},
            )
            value = str(payload.get("value", ""))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                with _lock:
                    _cache[key] = (now + READ_TTL, None)
                raise KeyVaultError(f"Secret {name} not found.") from exc
            raise KeyVaultError(f"Key Vault read failed for {name}: {exc}") from exc
        except (urllib.error.URLError, OSError, KeyVaultError):
            # IMDS unreachable — fall back to the CLI for the rest of the process.
            _imds_ok = False

    if value is None:
        try:
            value = _az_get(vault, name)
        except subprocess.CalledProcessError as exc:
            with _lock:
                _cache[key] = (now + READ_TTL, None)
            raise KeyVaultError(f"Secret {name} not found.") from exc

    with _lock:
        _cache[key] = (now + READ_TTL, value)
    return value


def set_secret(vault: str, name: str, value: str) -> None:
    global _imds_ok
    wrote = False
    if _imds_ok:
        try:
            token = _access_token()
            _request(
                _vault_url(vault, name),
                method="PUT",
                headers={"Authorization": f"Bearer {token}"},
                body={"value": value},
            )
            wrote = True
        except urllib.error.HTTPError as exc:
            raise KeyVaultError(f"Key Vault write failed for {name}: {exc}") from exc
        except (urllib.error.URLError, OSError, KeyVaultError):
            _imds_ok = False

    if not wrote:
        _az_set(vault, name, value)

    with _lock:
        _cache[f"{vault}/{name}"] = (time.time() + READ_TTL, value)


def invalidate() -> None:
    with _lock:
        _cache.clear()
