"""
Deleting an account must take the venue's people and their records with it.
Found 2026-10-07: every person row stayed behind (name, mobile, date of birth,
password hash), along with attendance (clock times, location, photo), leave
carry-over, swap requests and notification rows, because none of them carries
a venue_id for the cascade to find.
"""

from app import db as db_module
from tests.conftest import create_active_staff

# Tables that are not any venue's: reference data shared by every venue.
SHARED = {"app", "sqlite_sequence"}


def _fill(app, venue):
    person_id, membership_id, _ = create_active_staff(app, venue["id"], name="Stay Behind")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE person SET mobile = '07700900123', avatar_url = 'abc_face.jpg' WHERE id = ?",
                     (person_id,))
        shift_id = conn.execute(
            """INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time)
               VALUES (?, ?, '2026-10-01', '17:00', '23:00')""", (venue["id"], person_id)).lastrowid
        conn.execute(
            """INSERT INTO attendance (shift_id, clock_in_at, clock_in_lat, clock_in_lng, photo_url)
               VALUES (?, '2026-10-01 17:00:00', 52.6, 1.3, 'abc_clockin.jpg')""", (shift_id,))
        conn.commit()
    return person_id


def _tables_with_rows(conn):
    names = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {n for n in names if n not in SHARED
            and conn.execute(f"SELECT COUNT(*) AS n FROM {n}").fetchone()["n"]}


def test_deleting_the_only_venue_leaves_no_rows_and_removes_the_files(app, venue, monkeypatch):
    removed = []
    monkeypatch.setattr("app.media.delete_file", lambda kind, name: removed.append((kind, name)))
    _fill(app, venue)

    with app.app_context():
        conn = db_module.get_db()
        db_module.delete_venue_by_pub_id(conn, venue["pub_id"])
        assert _tables_with_rows(conn) == set()

    assert sorted(removed) == [("attendance_photo", "abc_clockin.jpg"), ("avatar", "abc_face.jpg")]


def test_someone_who_also_works_at_another_venue_is_kept(app, venue):
    person_id = _fill(app, venue)
    with app.app_context():
        conn = db_module.get_db()
        other = conn.execute(
            "INSERT INTO venue (pub_id, name, slug) VALUES (999, 'Other Venue', 'othervenue')").lastrowid
        conn.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')",
            (person_id, other))
        conn.commit()

        db_module.delete_venue_by_pub_id(conn, venue["pub_id"])

        row = conn.execute("SELECT name, avatar_url FROM person WHERE id = ?", (person_id,)).fetchone()
        assert row is not None and row["avatar_url"] == "abc_face.jpg"
