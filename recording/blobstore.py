"""Azure Blob upload and 6‑month retention via managed identity REST."""

from __future__ import annotations

import datetime as dt
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


def upload_file(
    get_secret: GetOptional,
    local_path: Path,
    *,
    blob_name: str,
) -> str:
    """Upload a file; returns the blob URL (no SAS)."""
    account, container = _account(get_secret)
    token = _imds_token()
    url = (
        f"https://{account}.blob.core.windows.net/{container}/"
        f"{urllib.parse.quote(blob_name)}"
    )
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
            "x-ms-version": "2021-08-06",
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
    return url


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
            "x-ms-version": "2021-08-06",
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
    url = (
        f"https://{account}.blob.core.windows.net/{container}/"
        f"{urllib.parse.quote(name)}"
    )
    req = urllib.request.Request(
        url,
        method="DELETE",
        headers={
            "Authorization": f"Bearer {token}",
            "x-ms-version": "2021-08-06",
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
