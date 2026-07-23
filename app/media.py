"""
Sensitive-media storage: staff avatars and attendance photos.

Mirrors pubpulse/app/main.py's upload pattern (UPLOAD_DIR under data/,
secrets.token_hex(4)-prefixed secure_filename naming) but deviates
deliberately on serving: avatars and attendance photos are more sensitive
than anything else the PubPulse family stores (spec §3's own sensitivity
note), so they're served through an authenticated route here rather than
Flask's plain unauthenticated-by-default /static/ — only the owning person
or an admin tier can fetch a given file.
"""

import secrets
from pathlib import Path

import flask
from werkzeug.utils import secure_filename

from app.rota_auth import register_identity, require_permission
from app.venue_scope import register_venue_scope

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
AVATAR_DIR = UPLOAD_DIR / "avatars"
ATTENDANCE_PHOTO_DIR = UPLOAD_DIR / "attendance_photos"

media_bp = flask.Blueprint("media", __name__, url_prefix="/v/<slug>/media")
register_venue_scope(media_bp)
register_identity(media_bp)


def save_avatar(file_storage) -> str:
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{secrets.token_hex(4)}_{secure_filename(file_storage.filename)}"
    file_storage.save(AVATAR_DIR / stored_filename)
    return stored_filename


def save_attendance_photo(file_storage) -> str:
    ATTENDANCE_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{secrets.token_hex(4)}_{secure_filename(file_storage.filename)}"
    file_storage.save(ATTENDANCE_PHOTO_DIR / stored_filename)
    return stored_filename


def delete_file(kind: str, stored_filename: str) -> None:
    directory = AVATAR_DIR if kind == "avatar" else ATTENDANCE_PHOTO_DIR
    try:
        (directory / stored_filename).unlink(missing_ok=True)
    except OSError:
        pass


@media_bp.route("/avatar/<path:stored_filename>")
@require_permission("app_admin", "rota_admin", "staff")
def avatar(stored_filename):
    return flask.send_from_directory(AVATAR_DIR, stored_filename)


@media_bp.route("/attendance-photo/<path:stored_filename>")
@require_permission("app_admin", "rota_admin")
def attendance_photo(stored_filename):
    # Human-reviewed evidence for a supervisor, not a staff self-view (spec
    # §6.1) — deliberately excludes the plain 'staff' tier.
    return flask.send_from_directory(ATTENDANCE_PHOTO_DIR, stored_filename)
