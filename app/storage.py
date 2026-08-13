"""
Storage for uploaded avatars/attendance photos, behind a small
backend-agnostic API so app.media never talks to R2 (or the disk) directly.
Mirrors pubpulse (PricePulse)'s storage.py pattern exactly (that repo's
commit 6ece1e5), simplified for images served straight to the browser
rather than parsed from a path (no ``local_copy`` context manager needed).

Two backends, chosen at import time by whether R2 is configured in the
environment:

  * R2 (production): files live in the shared ``pubpulse-uploads`` R2
    bucket, keyed by the same ``stored_filename`` already recorded on the
    person/attendance row, under this app's own prefix. The Render disk
    then only needs to hold the SQLite DB.

  * Local disk (dev, tests, and as a read *fallback*): files live in the
    caller's ``local_dir`` (app.media's AVATAR_DIR/ATTENDANCE_PHOTO_DIR),
    exactly as before this module existed.

Reads try R2 first and fall back to the local disk, so during the one-off
migration a file that hasn't been copied up to R2 yet still opens.
"""

from __future__ import annotations

import os
from pathlib import Path

# All four must be present to switch on the R2 backend; otherwise everything
# stays on local disk (so a dev box or CI with no R2 config just works).
_ACCOUNT = os.environ.get("R2_ACCOUNT_ID")
_BUCKET = os.environ.get("R2_UPLOADS_BUCKET")
_KEY_ID = os.environ.get("R2_UPLOADS_KEY_ID")
_SECRET = os.environ.get("R2_UPLOADS_SECRET")
# One bucket serves the whole family; objects are namespaced per app.
_PREFIX = os.environ.get("R2_UPLOADS_PREFIX", "rotapulse").strip("/")

_ENABLED = all([_ACCOUNT, _BUCKET, _KEY_ID, _SECRET])

_client = None


def r2_enabled() -> bool:
    """True when the R2 backend is configured (production)."""
    return _ENABLED


def _r2():
    """Lazily build the boto3 S3 client (so importing this module is cheap and
    dev/tests never need boto3 configured)."""
    global _client
    if _client is None:
        import boto3

        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{_ACCOUNT}.r2.cloudflarestorage.com",
            aws_access_key_id=_KEY_ID,
            aws_secret_access_key=_SECRET,
            region_name="auto",
        )
    return _client


def _key(stored_filename: str) -> str:
    return f"{_PREFIX}/{stored_filename}"


def save(stored_filename: str, data: bytes, local_dir) -> None:
    """Persist already-in-memory file bytes as ``stored_filename``. R2
    backend uploads it; local backend writes it into ``local_dir``."""
    if _ENABLED:
        _r2().put_object(Bucket=_BUCKET, Key=_key(stored_filename), Body=data)
        return
    dest = Path(local_dir) / stored_filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def read_bytes(stored_filename: str, local_dir):
    """Return the file's bytes for serving to the browser, or None if it
    can't be found in either backend. R2 first, then a local-disk fallback
    for files not yet migrated."""
    # Defence in depth against path traversal: a stored_filename is always a
    # single flat token, so any path separator / parent-dir hop / NUL is bogus
    # and must never reach the `Path(local_dir) / stored_filename` join below
    # (nor the R2 key). Callers in app.media reject these too; this closes the
    # sink itself.
    if (
        not stored_filename
        or "/" in stored_filename
        or "\\" in stored_filename
        or ".." in stored_filename
        or "\x00" in stored_filename
    ):
        return None
    if _ENABLED:
        data = _get_r2_bytes(stored_filename)
        if data is not None:
            return data
    p = Path(local_dir) / stored_filename
    return p.read_bytes() if p.exists() else None


def delete(stored_filename: str, local_dir) -> None:
    """Best-effort delete from both backends (a leftover file is not a
    data-integrity problem, so failures are swallowed — same stance as the
    old on-disk unlink)."""
    if _ENABLED:
        try:
            _r2().delete_object(Bucket=_BUCKET, Key=_key(stored_filename))
        except Exception:
            pass
    try:
        (Path(local_dir) / stored_filename).unlink(missing_ok=True)
    except OSError:
        pass


def _get_r2_bytes(stored_filename: str):
    """Fetch an object's bytes from R2, or None if it isn't there (a missing
    key is an expected 'not migrated yet' case, not an error)."""
    from botocore.exceptions import ClientError

    try:
        obj = _r2().get_object(Bucket=_BUCKET, Key=_key(stored_filename))
        return obj["Body"].read()
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
