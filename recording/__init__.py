"""Scheduled Zoom Meeting SDK recording → Azure Blob.

Modules are intentionally small and single-purpose so the SDK binary,
scheduler, storage, and UI can evolve independently:

  models.py    — schedule / slot data shapes
  store.py     — persist schedule on the VM
  clock.py     — IST window matching
  meeting.py   — is the Zoom meeting running?
  blobstore.py — upload + 6‑month retention
  recorder/    — Meeting SDK adapter (swap implementation without touching UI)
  pipeline.py  — one “tick” of the scheduler
"""

from __future__ import annotations

__all__ = ["pipeline", "store", "models"]
