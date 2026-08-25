from datetime import date, datetime, timedelta

from app import db as db_module
from app.uk_time import uk_now
from tests.conftest import create_active_staff, login_as_person


def _create_shift_for(app, venue_id, person_id, shift_date=None):
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, ?, ?, 'scheduled')",
            (venue_id, person_id, shift_date or date.today().isoformat(), "09:00", "17:00"),
        )
        conn.commit()
        return cur.lastrowid


def test_clock_in_stores_uk_local_time_not_raw_utc(app, client, venue):
    """Real report, 2026-08-19: a clock-in was recorded an hour behind UK
    time (the server runs in UTC; the app used to stamp attendance via
    SQLite's own datetime('now'), which is always UTC — see app/uk_time.py
    for the full story). Regression guard: clock_in_at should land within
    a couple of seconds of app.uk_time.uk_now(), not of naive UTC — if
    someone ever reverts to datetime('now')/datetime.utcnow() here, this
    fails immediately rather than silently drifting by an hour every BST."""
    person_id, _m, _e = create_active_staff(app, venue["id"])
    shift_id = _create_shift_for(app, venue["id"], person_id)
    login_as_person(client, person_id)

    before = uk_now()
    client.post(f"/v/{venue['slug']}/staff/shift/{shift_id}/clock-in", data={"lat": "52.6", "lng": "1.3"})
    after = uk_now()

    with app.app_context():
        conn = db_module.get_db()
        stored = datetime.fromisoformat(
            conn.execute("SELECT clock_in_at FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()["clock_in_at"]
        )
    assert before - timedelta(seconds=2) <= stored <= after + timedelta(seconds=2)


def test_clock_in_rejected_for_a_future_shift(app, client, venue):
    """Real report, 2026-08-18: nothing stopped clocking in for a shift
    days ahead straight from "My shifts" (which lists up to 3 weeks out)."""
    person_id, _m, _e = create_active_staff(app, venue["id"])
    future_date = (date.today() + timedelta(days=5)).isoformat()
    shift_id = _create_shift_for(app, venue["id"], person_id, future_date)
    login_as_person(client, person_id)

    resp = client.post(
        f"/v/{venue['slug']}/staff/shift/{shift_id}/clock-in",
        data={"lat": "52.6", "lng": "1.3"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"only clock in on the day" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        att = conn.execute("SELECT * FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert att is None  # nothing recorded


def test_clock_in_rejected_for_a_past_shift(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"])
    past_date = (date.today() - timedelta(days=3)).isoformat()
    shift_id = _create_shift_for(app, venue["id"], person_id, past_date)
    login_as_person(client, person_id)

    resp = client.post(
        f"/v/{venue['slug']}/staff/shift/{shift_id}/clock-in",
        data={"lat": "52.6", "lng": "1.3"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"only clock in on the day" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        att = conn.execute("SELECT * FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert att is None


def test_clock_in_within_radius_confirms_location(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"])
    shift_id = _create_shift_for(app, venue["id"], person_id)
    login_as_person(client, person_id)

    # venue fixture is at (52.6, 1.3) — same coordinates, distance 0.
    resp = client.post(
        f"/v/{venue['slug']}/staff/shift/{shift_id}/clock-in",
        data={"lat": "52.6", "lng": "1.3"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        att = conn.execute("SELECT * FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert att["clock_in_location_confirmed"] == 1
        assert att["clock_in_at"] is not None


def test_clock_in_far_away_flags_unconfirmed_but_still_succeeds(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"])
    shift_id = _create_shift_for(app, venue["id"], person_id)
    login_as_person(client, person_id)

    # Roughly London, ~150km from the venue's (52.6, 1.3) fixture coords.
    resp = client.post(
        f"/v/{venue['slug']}/staff/shift/{shift_id}/clock-in",
        data={"lat": "51.5", "lng": "-0.1"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        att = conn.execute("SELECT * FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert att["clock_in_location_confirmed"] == 0
        assert att["clock_in_at"] is not None  # still clocked in — not blocked


def test_declining_geolocation_still_clocks_in(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"])
    shift_id = _create_shift_for(app, venue["id"], person_id)
    login_as_person(client, person_id)

    resp = client.post(f"/v/{venue['slug']}/staff/shift/{shift_id}/clock-in", data={}, follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        att = conn.execute("SELECT * FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert att["clock_in_location_confirmed"] is None
        assert att["clock_in_at"] is not None


def test_clock_out_records_timestamp(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"])
    shift_id = _create_shift_for(app, venue["id"], person_id)
    login_as_person(client, person_id)

    client.post(f"/v/{venue['slug']}/staff/shift/{shift_id}/clock-in", data={"lat": "52.6", "lng": "1.3"})
    client.post(f"/v/{venue['slug']}/staff/shift/{shift_id}/clock-out", data={"lat": "52.6", "lng": "1.3"})

    with app.app_context():
        conn = db_module.get_db()
        att = conn.execute("SELECT * FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert att["clock_out_at"] is not None
