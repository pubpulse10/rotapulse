"""Shifts that run past midnight — the "Cinderella problem" (real report,
2026-09-13): staff couldn't clock out of a late shift because it vanished
from My shifts the moment the date ticked over, and clocking out of a
17:00-00:00 shift just before midnight was flagged as a day late.
"""

from datetime import datetime, timedelta

from app import db as db_module
from app.date_format import variance_label
from app.uk_time import planned_datetime, uk_today
from tests.conftest import create_active_staff, login_as_person


def _shift(app, venue_id, person_id, shift_date, start_time="17:00", end_time="00:00",
           clock_in_at=None, clock_out_at=None):
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, ?, ?, 'scheduled')",
            (venue_id, person_id, shift_date, start_time, end_time),
        ).lastrowid
        if clock_in_at:
            conn.execute(
                "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at) VALUES (?, ?, ?)",
                (shift_id, clock_in_at, clock_out_at),
            )
        conn.commit()
        return shift_id


def _yesterday():
    return (uk_today() - timedelta(days=1)).isoformat()


# ---------- My shifts after midnight ----------


def test_last_nights_shift_still_clocked_in_stays_on_my_shifts(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Cinderella")
    yesterday = _yesterday()
    shift_id = _shift(app, venue["id"], person_id, yesterday, clock_in_at=f"{yesterday} 17:00:00")
    login_as_person(client, person_id)

    resp = client.get(f"/v/{venue['slug']}/staff/")
    assert resp.status_code == 200
    assert f'/staff/shift/{shift_id}"'.encode() in resp.data
    assert b"Clocked in" in resp.data
    # Still clocked in to last night's shift, so this must NOT be offered.
    assert b"Start an unplanned shift" not in resp.data


def test_last_nights_finished_shift_drops_off_my_shifts(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Went Home")
    yesterday = _yesterday()
    shift_id = _shift(
        app, venue["id"], person_id, yesterday,
        clock_in_at=f"{yesterday} 17:00:00", clock_out_at=f"{uk_today().isoformat()} 00:05:00",
    )
    login_as_person(client, person_id)

    resp = client.get(f"/v/{venue['slug']}/staff/")
    assert f'/staff/shift/{shift_id}"'.encode() not in resp.data


def test_last_nights_never_started_shift_drops_off_my_shifts(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="No Show")
    shift_id = _shift(app, venue["id"], person_id, _yesterday())
    login_as_person(client, person_id)

    resp = client.get(f"/v/{venue['slug']}/staff/")
    assert f'/staff/shift/{shift_id}"'.encode() not in resp.data


def test_an_older_forgotten_clock_out_is_not_listed(app, client, venue):
    """Only last night's shift carries over. A days-old open shift is an admin
    correction — offering a Clock out tap would book days of hours to payroll."""
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Forgot Days Ago")
    two_days_ago = (uk_today() - timedelta(days=2)).isoformat()
    shift_id = _shift(app, venue["id"], person_id, two_days_ago, clock_in_at=f"{two_days_ago} 17:00:00")
    login_as_person(client, person_id)

    resp = client.get(f"/v/{venue['slug']}/staff/")
    assert f'/staff/shift/{shift_id}"'.encode() not in resp.data


def test_staff_can_clock_out_of_last_nights_shift_after_midnight(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Clock Out Late")
    yesterday = _yesterday()
    shift_id = _shift(app, venue["id"], person_id, yesterday, clock_in_at=f"{yesterday} 17:00:00")
    login_as_person(client, person_id)

    detail = client.get(f"/v/{venue['slug']}/staff/shift/{shift_id}")
    assert b"Clock out" in detail.data

    resp = client.post(f"/v/{venue['slug']}/staff/shift/{shift_id}/clock-out", data={}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        conn = db_module.get_db()
        att = conn.execute("SELECT clock_out_at FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert att["clock_out_at"] is not None


def test_cannot_start_an_ad_hoc_shift_while_still_clocked_in_to_last_nights(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Double Booked Overnight")
    yesterday = _yesterday()
    shift_id = _shift(app, venue["id"], person_id, yesterday, clock_in_at=f"{yesterday} 17:00:00")
    login_as_person(client, person_id)

    resp = client.post(f"/v/{venue['slug']}/staff/shift/ad-hoc/clock-in", data={}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"still clocked in to last night" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        count = conn.execute("SELECT COUNT(*) AS n FROM shift WHERE person_id = ?", (person_id,)).fetchone()["n"]
        assert count == 1  # no ad-hoc shift piled on top


def test_a_finished_shift_last_night_does_not_block_starting_an_ad_hoc_one(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Back Again")
    yesterday = _yesterday()
    _shift(
        app, venue["id"], person_id, yesterday,
        clock_in_at=f"{yesterday} 17:00:00", clock_out_at=f"{uk_today().isoformat()} 00:05:00",
    )
    login_as_person(client, person_id)

    resp = client.post(f"/v/{venue['slug']}/staff/shift/ad-hoc/clock-in", data={}, follow_redirects=True)
    assert b"still clocked in" not in resp.data
    assert b"admin approval" in resp.data


# ---------- Clock-out variance across midnight ----------


def _clock_out_at(app, client, venue, monkeypatch, name, now):
    person_id, _m, _e = create_active_staff(app, venue["id"], name=name)
    shift_date = "2026-09-12"
    shift_id = _shift(app, venue["id"], person_id, shift_date, clock_in_at=f"{shift_date} 17:00:00")
    monkeypatch.setattr("app.staff_portal.uk_now", lambda: now)
    login_as_person(client, person_id)
    client.post(f"/v/{venue['slug']}/staff/shift/{shift_id}/clock-out", data={})
    with app.app_context():
        conn = db_module.get_db()
        return conn.execute("SELECT variance_flag FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()["variance_flag"]


def test_clocking_out_of_a_midnight_shift_just_before_midnight_is_not_flagged(app, client, venue, monkeypatch):
    flag = _clock_out_at(app, client, venue, monkeypatch, "Five To", datetime(2026, 9, 12, 23, 55))
    assert flag == 0


def test_clocking_out_of_a_midnight_shift_just_after_midnight_is_not_flagged(app, client, venue, monkeypatch):
    flag = _clock_out_at(app, client, venue, monkeypatch, "Ten Past", datetime(2026, 9, 13, 0, 10))
    assert flag == 0


def test_clocking_out_an_hour_after_a_midnight_finish_is_still_flagged(app, client, venue, monkeypatch):
    flag = _clock_out_at(app, client, venue, monkeypatch, "Stayed On", datetime(2026, 9, 13, 1, 0))
    assert flag == 1


# ---------- Helpers ----------


def test_planned_end_rolls_to_next_day_when_before_start():
    assert planned_datetime("2026-09-12", "00:00", "17:00") == datetime(2026, 9, 13, 0, 0)
    assert planned_datetime("2026-09-12", "02:00", "20:00") == datetime(2026, 9, 13, 2, 0)


def test_planned_end_same_day_when_after_start():
    assert planned_datetime("2026-09-12", "23:00", "17:00") == datetime(2026, 9, 12, 23, 0)


def test_planned_end_equal_to_start_does_not_roll():
    # Ad-hoc shifts are created with start_time == end_time as a placeholder.
    assert planned_datetime("2026-09-12", "17:00", "17:00") == datetime(2026, 9, 12, 17, 0)


def test_planned_start_is_on_the_shift_date():
    assert planned_datetime("2026-09-12", "17:00") == datetime(2026, 9, 12, 17, 0)


def test_variance_label_midnight_shift_clocked_out_just_before_midnight_is_on_time():
    assert variance_label("2026-09-12 23:55:00", "00:00", "2026-09-12", "17:00") is None


def test_variance_label_midnight_shift_clocked_out_an_hour_after_is_late():
    assert variance_label("2026-09-13 01:00:00", "00:00", "2026-09-12", "17:00") == "Late"


def test_variance_label_without_shift_date_keeps_old_behaviour():
    assert variance_label("2026-09-12 17:30:00", "17:00") == "Late"
