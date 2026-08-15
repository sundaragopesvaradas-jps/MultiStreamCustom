"""Azure Blob upload and 6‑month retention via managed identity REST."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

IMDS = (
    "http://169.254.169.254/metadata/identity/oauth2/token"
    "?api-version=2018-02-01&resource=https%3A%2F%2Fstorage.azure.com"
)

# User-delegation SAS version — must match the string-to-sign layout below.
SAS_VERSION = "2021-08-06"
# How long an emailed download link remains usable.
DEFAULT_DOWNLOAD_DAYS = 7

GetOptional = Callable[[str], str | None]


class BlobError(Exception):
    pass


def _imds_token() -> str:
    req = urllib.request.Request(IMDS, headers={"Metadata": "true"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    token = payload.get("access_token")
    if not token:
        raise BlobError("Managed identity returned no storage token.")
    return token


def _account(get_secret: GetOptional) -> tuple[str, str]:
    account = (
        get_secret("recording-storage-account")
        or os.environ.get("RECORDING_STORAGE_ACCOUNT")
        or ""
    ).strip()
    container = (
        get_secret("recording-storage-container")
        or os.environ.get("RECORDING_STORAGE_CONTAINER")
        or "recordings"
    ).strip()
    if not account:
        raise BlobError(
            "recording-storage-account is not set in Key Vault. "
            "Create a storage account and save the name first."
        )
    return account, container


def _blob_url(account: str, container: str, blob_name: str) -> str:
    return (
        f"https://{account}.blob.core.windows.net/{container}/"
        f"{urllib.parse.quote(blob_name)}"
    )


def upload_file(
    get_secret: GetOptional,
    local_path: Path,
    *,
    blob_name: str,
) -> str:
    """Upload a file; returns a time-limited download URL (user-delegation SAS)."""
    account, container = _account(get_secret)
    token = _imds_token()
    url = _blob_url(account, container, blob_name)
    data = local_path.read_bytes()
    content_type = "application/octet-stream"
    if local_path.suffix.lower() == ".mp4":
        content_type = "video/mp4"
    elif local_path.suffix.lower() in {".mkv", ".webm"}:
        content_type = f"video/{local_path.suffix.lower().lstrip('.')}"

    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "x-ms-version": SAS_VERSION,
            "x-ms-blob-type": "BlockBlob",
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BlobError(f"Blob upload failed ({exc.code}): {detail}") from exc

    try:
        return download_url(
            get_secret,
            blob_name,
            valid_for_days=DEFAULT_DOWNLOAD_DAYS,
            token=token,
        )
    except BlobError:
        # Upload succeeded; fall back to the bare URL so the alert still names the file.
        return url


def download_url(
    get_secret: GetOptional,
    blob_name: str,
    *,
    valid_for_days: int = DEFAULT_DOWNLOAD_DAYS,
    token: str | None = None,
) -> str:
    """Return a read-only HTTPS URL that works without Azure login for a few days.

    The storage account keeps public access off. This signs a user-delegation SAS
    with the VM's managed identity so the email link can open in a browser.
    """
    account, container = _account(get_secret)
    bearer = token or _imds_token()
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    # Key lifetime must cover the SAS; Azure caps user-delegation keys at 7 days.
    key_days = min(max(valid_for_days, 1), 7)
    key_start = (now - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    key_expiry = (now + dt.timedelta(days=key_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sas_start = key_start
    sas_expiry = key_expiry

    key = _user_delegation_key(account, bearer, key_start, key_expiry)
    signed_resource = "b"
    permissions = "r"
    protocol = "https"
    canonical = f"/blob/{account}/{container}/{blob_name}"

    string_to_sign = "\n".join(
        [
            permissions,
            sas_start,
            sas_expiry,
            canonical,
            key["oid"],
            key["tid"],
            key["start"],
            key["expiry"],
            key["service"],
            key["version"],
            "",  # signedAuthorizedUserObjectId
            "",  # signedUnauthorizedUserObjectId
            "",  # signedCorrelationId
            "",  # signedIP
            protocol,
            SAS_VERSION,
            signed_resource,
            "",  # snapshot
            "",  # encryption scope
            "",  # rscc
            "",  # rscd
            "",  # rsce
            "",  # rscl
            "",  # rsct
        ]
    )
    digest = hmac.new(
        base64.b64decode(key["value"]),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature = urllib.parse.quote(base64.b64encode(digest).decode("ascii"), safe="")

    query = (
        f"sp={permissions}"
        f"&st={urllib.parse.quote(sas_start, safe='')}"
        f"&se={urllib.parse.quote(sas_expiry, safe='')}"
        f"&skoid={key['oid']}"
        f"&sktid={key['tid']}"
        f"&skt={urllib.parse.quote(key['start'], safe='')}"
        f"&ske={urllib.parse.quote(key['expiry'], safe='')}"
        f"&sks={key['service']}"
        f"&skv={key['version']}"
        f"&spr={protocol}"
        f"&sv={SAS_VERSION}"
        f"&sr={signed_resource}"
        f"&sig={signature}"
    )
    return f"{_blob_url(account, container, blob_name)}?{query}"


def _user_delegation_key(
    account: str,
    bearer: str,
    start: str,
    expiry: str,
) -> dict[str, str]:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<KeyInfo><Start>{start}</Start><Expiry>{expiry}</Expiry></KeyInfo>"
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://{account}.blob.core.windows.net/?restype=service&comp=userdelegationkey",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {bearer}",
            "x-ms-version": SAS_VERSION,
            "Content-Type": "application/xml",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BlobError(
            f"Could not create download link ({exc.code}): {detail}"
        ) from exc

    fields = {
        "oid": _tag(xml, "SignedOid") or "",
        "tid": _tag(xml, "SignedTid") or "",
        "start": _tag(xml, "SignedStart") or "",
        "expiry": _tag(xml, "SignedExpiry") or "",
        "service": _tag(xml, "SignedService") or "",
        "version": _tag(xml, "SignedVersion") or "",
        "value": _tag(xml, "Value") or "",
    }
    if not all(fields.values()):
        raise BlobError(f"User delegation key response incomplete: {xml[:300]}")
    return fields


def purge_older_than(get_secret: GetOptional, *, days: int = 180) -> list[str]:
    """Delete blobs whose last-modified is older than `days`. Returns deleted names."""
    account, container = _account(get_secret)
    token = _imds_token()
    list_url = (
        f"https://{account}.blob.core.windows.net/{container}"
        f"?restype=container&comp=list"
    )
    req = urllib.request.Request(
        list_url,
        headers={
            "Authorization": f"Bearer {token}",
            "x-ms-version": SAS_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            xml = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BlobError(f"Blob list failed ({exc.code}): {detail}") from exc

    # Minimal XML scrape — avoid adding an XML dependency.
    deleted: list[str] = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    parts = xml.split("<Blob>")
    for part in parts[1:]:
        name = _tag(part, "Name")
        modified = _tag(part, "Last-Modified")
        if not name or not modified:
            continue
        try:
            when = dt.datetime.strptime(modified, "%a, %d %b %Y %H:%M:%S %Z")
            when = when.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if when >= cutoff:
            continue
        _delete_blob(account, container, name, token)
        deleted.append(name)
    return deleted


def _tag(block: str, name: str) -> str | None:
    open_tag, close_tag = f"<{name}>", f"</{name}>"
    start = block.find(open_tag)
    end = block.find(close_tag)
    if start < 0 or end < 0:
        return None
    return block[start + len(open_tag) : end]


def _delete_blob(account: str, container: str, name: str, token: str) -> None:
    url = _blob_url(account, container, name)
    req = urllib.request.Request(
        url,
        method="DELETE",
        headers={
            "Authorization": f"Bearer {token}",
            "x-ms-version": SAS_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return
        detail = exc.read().decode("utf-8", errors="replace")
        raise BlobError(f"Blob delete failed for {name} ({exc.code}): {detail}") from exc
