"""
Covers app/media.py (validation, save/serve/delete on the local-disk
backend, which is what every other test in this suite implicitly exercises
since R2 is never configured in tests) and app/storage.py's R2 codepath in
isolation (mocked boto3 — no real network/credentials needed or wanted in
a test run).
"""

import base64
import io

import pytest

from app import storage
from app.media import (
    ATTENDANCE_PHOTO_DIR,
    AVATAR_DIR,
    delete_file,
    save_attendance_photo,
    save_avatar,
)
from tests.conftest import create_active_staff, login_as_pub

# A real 1x1 transparent PNG (matches the magic-byte check in _validate_image).
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _png_file(name="test.png"):
    return (io.BytesIO(_PNG_BYTES), name)


class FakeStream:
    def __init__(self, data):
        self._data = data

    def read(self, n=-1):
        return self._data if n == -1 else self._data[:n]

    def seek(self, pos):
        pass


class FakeFileStorage:
    """A minimal stand-in for werkzeug's FileStorage, avoiding the need to
    spin up a full test-client multipart POST just to exercise save_avatar/
    save_attendance_photo directly."""

    def __init__(self, data: bytes, filename: str):
        self.filename = filename
        self.stream = FakeStream(data)


def test_save_and_read_avatar_round_trips_on_local_disk(app):
    with app.app_context():
        stored = save_avatar(FakeFileStorage(_PNG_BYTES, "me.png"))
        data = storage.read_bytes(stored, AVATAR_DIR)
    assert data == _PNG_BYTES


def test_save_attendance_photo_round_trips(app):
    with app.app_context():
        stored = save_attendance_photo(FakeFileStorage(_PNG_BYTES, "shift.png"))
        data = storage.read_bytes(stored, ATTENDANCE_PHOTO_DIR)
    assert data == _PNG_BYTES


def test_delete_file_removes_it(app):
    with app.app_context():
        stored = save_avatar(FakeFileStorage(_PNG_BYTES, "me.png"))
        delete_file("avatar", stored)
        assert storage.read_bytes(stored, AVATAR_DIR) is None


def test_reject_non_image_extension(app):
    with app.app_context():
        with pytest.raises(Exception):
            save_avatar(FakeFileStorage(b"not an image", "shell.exe"))


def test_reject_mismatched_magic_bytes(app):
    """A file renamed to .png that isn't really one — extension alone must
    not be trusted."""
    with app.app_context():
        with pytest.raises(Exception):
            save_avatar(FakeFileStorage(b"<html>not a real png</html>", "fake.png"))


def test_avatar_route_serves_uploaded_file_as_attachment(app, client, venue):
    person_id, membership_id, email = create_active_staff(app, venue["id"], name="Alex")
    with app.app_context():
        stored = save_avatar(FakeFileStorage(_PNG_BYTES, "alex.png"))
        conn = __import__("app.db", fromlist=["get_db"]).get_db()
        conn.execute("UPDATE person SET avatar_url = ? WHERE id = ?", (stored, person_id))
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/media/avatar/{stored}")
    assert resp.status_code == 200
    assert resp.data == _PNG_BYTES
    assert resp.headers["Content-Type"] == "image/png"
    assert "attachment" in resp.headers["Content-Disposition"]


def test_avatar_route_404s_for_unknown_file(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/media/avatar/does-not-exist.png")
    assert resp.status_code == 404


# --- storage.py's R2 backend, mocked (no real network/credentials) --------


def test_storage_uses_r2_when_configured(monkeypatch):
    monkeypatch.setattr(storage, "_ENABLED", True)
    monkeypatch.setattr(storage, "_BUCKET", "test-bucket")

    put_calls = []
    get_calls = []
    delete_calls = []

    class FakeR2Client:
        def put_object(self, Bucket, Key, Body):
            put_calls.append((Bucket, Key, Body))

        def get_object(self, Bucket, Key):
            get_calls.append((Bucket, Key))
            return {"Body": io.BytesIO(b"r2-bytes")}

        def delete_object(self, Bucket, Key):
            delete_calls.append((Bucket, Key))

    monkeypatch.setattr(storage, "_r2", lambda: FakeR2Client())

    storage.save("file.png", b"hello", "/unused/local/dir")
    assert put_calls == [("test-bucket", "rotapulse/file.png", b"hello")]

    data = storage.read_bytes("file.png", "/unused/local/dir")
    assert data == b"r2-bytes"
    assert get_calls == [("test-bucket", "rotapulse/file.png")]

    storage.delete("file.png", "/unused/local/dir")
    assert delete_calls == [("test-bucket", "rotapulse/file.png")]


def test_storage_read_falls_back_to_local_disk_when_r2_misses(monkeypatch, tmp_path):
    from botocore.exceptions import ClientError

    monkeypatch.setattr(storage, "_ENABLED", True)
    monkeypatch.setattr(storage, "_BUCKET", "test-bucket")

    class MissingKeyR2Client:
        def get_object(self, Bucket, Key):
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    monkeypatch.setattr(storage, "_r2", lambda: MissingKeyR2Client())

    local_file = tmp_path / "not-yet-migrated.png"
    local_file.write_bytes(b"still-on-disk")

    data = storage.read_bytes("not-yet-migrated.png", tmp_path)
    assert data == b"still-on-disk"
