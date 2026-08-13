"""
Sensitive-media storage: staff avatars and attendance photos.

Mirrors pubpulse/app/main.py's upload pattern (UPLOAD_DIR under data/,
secrets.token_hex(4)-prefixed secure_filename naming) but deviates
deliberately on serving: avatars and attendance photos are more sensitive
than anything else the PubPulse family stores (spec §3's own sensitivity
note), so they're served through an authenticated route here rather than
Flask's plain unauthenticated-by-default /static/ — only the owning person
or an admin tier can fetch a given file.

Actual bytes go through app/storage.py (R2 in production, local disk in
dev/tests, R2-with-disk-fallback on read) rather than touching AVATAR_DIR/
ATTENDANCE_PHOTO_DIR directly — the Render disk isn't backed up, unlike the
SQLite DB (which streams to R2 via Litestream), so this closes that gap the
same way pubpulse (PricePulse)'s invoice PDFs already did.
"""

import mimetypes
import secrets
from pathlib import Path

import flask
from werkzeug.utils import secure_filename

from app import storage
from app.db import get_db
from app.rota_auth import register_identity, require_permission
from app.venue_scope import register_venue_scope

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
AVATAR_DIR = UPLOAD_DIR / "avatars"
ATTENDANCE_PHOTO_DIR = UPLOAD_DIR / "attendance_photos"

media_bp = flask.Blueprint("media", __name__, url_prefix="/v/<slug>/media")
register_venue_scope(media_bp)
register_identity(media_bp)

# Only these image types are accepted, checked by BOTH extension and leading
# magic bytes — an attacker can rename anything to .png, so the extension
# alone is not trusted.
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
_MAGIC_PREFIXES = (
    b"\x89PNG\r\n\x1a\n",   # PNG
    b"\xff\xd8\xff",         # JPEG
)


def _validate_image(file_storage) -> None:
    """Reject anything that isn't a whitelisted image by extension AND by the
    file's own leading bytes. Aborts the request with 400 on failure."""
    filename = file_storage.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        flask.abort(400, "Unsupported image type.")
    head = file_storage.stream.read(12)
    file_storage.stream.seek(0)
    # WebP is a RIFF container: bytes 0-3 'RIFF', bytes 8-11 'WEBP'.
    is_webp = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if not (head.startswith(_MAGIC_PREFIXES) or is_webp):
        flask.abort(400, "That file doesn't look like a valid image.")


def save_avatar(file_storage) -> str:
    _validate_image(file_storage)
    stored_filename = f"{secrets.token_hex(4)}_{secure_filename(file_storage.filename)}"
    storage.save(stored_filename, file_storage.stream.read(), AVATAR_DIR)
    return stored_filename


def save_attendance_photo(file_storage) -> str:
    _validate_image(file_storage)
    stored_filename = f"{secrets.token_hex(4)}_{secure_filename(file_storage.filename)}"
    storage.save(stored_filename, file_storage.stream.read(), ATTENDANCE_PHOTO_DIR)
    return stored_filename


def delete_file(kind: str, stored_filename: str) -> None:
    directory = AVATAR_DIR if kind == "avatar" else ATTENDANCE_PHOTO_DIR
    storage.delete(stored_filename, directory)


def _is_safe_stored_filename(stored_filename: str) -> bool:
    """A stored media filename is always a single flat token (token_hex + '_' +
    secure_filename — no directory part). Anything carrying a path separator, a
    parent-dir hop, or a NUL is a traversal attempt, never a real key, so it's
    rejected before any filesystem/R2 access. The route's <path:> converter
    otherwise lets slashes through."""
    return not (
        not stored_filename
        or "/" in stored_filename
        or "\\" in stored_filename
        or ".." in stored_filename
        or "\x00" in stored_filename
    )


def _avatar_belongs_to_venue(stored_filename: str) -> bool:
    """True only if this avatar filename is recorded against a person with a
    membership at the current venue — stops venue A fetching venue B's avatar
    by guessing its name (the filename itself carries no venue in it)."""
    row = get_db().execute(
        """SELECT 1 FROM person
           JOIN venue_membership ON venue_membership.person_id = person.id
           WHERE person.avatar_url = ? AND venue_membership.venue_id = ?
           LIMIT 1""",
        (stored_filename, flask.g.venue["id"]),
    ).fetchone()
    return row is not None


def _attendance_photo_belongs_to_venue(stored_filename: str) -> bool:
    """True only if this attendance-photo filename belongs to a shift at the
    current venue — same cross-venue-read guard as avatars."""
    row = get_db().execute(
        """SELECT 1 FROM attendance
           JOIN shift ON shift.id = attendance.shift_id
           WHERE attendance.photo_url = ? AND shift.venue_id = ?
           LIMIT 1""",
        (stored_filename, flask.g.venue["id"]),
    ).fetchone()
    return row is not None


def _serve(stored_filename: str, local_dir):
    if not _is_safe_stored_filename(stored_filename):
        flask.abort(404)
    data = storage.read_bytes(stored_filename, local_dir)
    if data is None:
        flask.abort(404)
    content_type = mimetypes.guess_type(stored_filename)[0] or "application/octet-stream"
    resp = flask.Response(data, mimetype=content_type)
    # as_attachment-equivalent: forces Content-Disposition: attachment so an
    # uploaded file can never be rendered inline in the browser (defence
    # against a file that slipped through as active content). Confirmed live
    # this doesn't break the <img> avatars on rota/week.html — browsers still
    # paint an <img> fetch as an image regardless of Content-Disposition; it
    # only affects direct top-level navigation to the URL.
    resp.headers["Content-Disposition"] = f"attachment; filename={secure_filename(stored_filename)}"
    return resp


@media_bp.route("/avatar/<path:stored_filename>")
@require_permission("app_admin", "rota_admin", "staff")
def avatar(stored_filename):
    if not _avatar_belongs_to_venue(stored_filename):
        flask.abort(404)
    return _serve(stored_filename, AVATAR_DIR)


@media_bp.route("/attendance-photo/<path:stored_filename>")
@require_permission("app_admin", "rota_admin")
def attendance_photo(stored_filename):
    # Human-reviewed evidence for a supervisor, not a staff self-view (spec
    # §6.1) — deliberately excludes the plain 'staff' tier.
    if not _attendance_photo_belongs_to_venue(stored_filename):
        flask.abort(404)
    return _serve(stored_filename, ATTENDANCE_PHOTO_DIR)
