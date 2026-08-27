from datetime import date, timedelta

from app import db as db_module
from tests.conftest import create_active_staff, login_as_pub


def test_cell_panel_shows_actual_clock_in_and_out_times(app, client, venue):
    """Previously only ever shown to the staff member themselves
    (app/staff_portal.py) — an admin clicking a shift on the rota grid saw
    only the scheduled times, never what actually happened."""
    person_id, _m, _e = create_active_staff(app, venue["id"], name="AttendanceCellTest")
    today = date.today().isoformat()
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id, today),
        ).lastrowid
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at, variance_flag) VALUES (?, ?, ?, 1)",
            (shift_id, f"{today}T09:25:00", f"{today}T17:20:00"),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/rota/cell/{person_id}/{today}")
    assert resp.status_code == 200
    assert b"09:25" in resp.data
    assert b"17:20" in resp.data
    assert b"Late" in resp.data


def test_cell_panel_shows_not_clocked_in_yet_when_no_attendance_row(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="NoAttendanceTest")
    today = date.today().isoformat()
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id, today),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/rota/cell/{person_id}/{today}")
    assert resp.status_code == 200
    # No attendance row at all -> the "Actual:" summary block doesn't render.
    assert b"Actual:" not in resp.data


# ---------- Admin editing clock times (real report, 2026-08-19: no way to ----------
# ---------- fix a clock time when someone forgot to clock in or out)     ----------


def _shift_no_attendance(app, venue_id, person_id, shift_date):
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue_id, person_id, shift_date),
        )
        conn.commit()
        return cur.lastrowid


def test_admin_can_set_clock_times_on_a_shift_with_no_attendance_row(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="ForgotToClockIn")
    today = date.today().isoformat()
    shift_id = _shift_no_attendance(app, venue["id"], person_id, today)

    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/attendance",
        data={"clock_in_time": "09:05", "clock_out_time": "17:10"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT clock_in_at, clock_out_at FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert row["clock_in_at"] == f"{today} 09:05:00"
        assert row["clock_out_at"] == f"{today} 17:10:00"


def test_admin_can_edit_clock_times_on_an_existing_attendance_row_without_touching_other_fields(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="WrongClockTime")
    today = date.today().isoformat()
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id, today),
        ).lastrowid
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at, photo_url, variance_flag) VALUES (?, ?, ?, 'keep-me.jpg', 1)",
            (shift_id, f"{today} 09:25:00", f"{today} 17:20:00"),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/attendance",
        data={"clock_in_time": "09:00", "clock_out_time": "17:00"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT * FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert row["clock_in_at"] == f"{today} 09:00:00"
        assert row["clock_out_at"] == f"{today} 17:00:00"
        assert row["photo_url"] == "keep-me.jpg"
        assert row["variance_flag"] == 1


def test_admin_editing_an_overnight_shift_rolls_clock_out_onto_the_next_day(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="OvernightShift")
    today = date.today().isoformat()
    shift_id = _shift_no_attendance(app, venue["id"], person_id, today)

    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/attendance",
        data={"clock_in_time": "22:00", "clock_out_time": "02:00"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT clock_in_at, clock_out_at FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert row["clock_in_at"] == f"{today} 22:00:00"
        assert row["clock_out_at"] == f"{tomorrow} 02:00:00"


def test_admin_can_clear_a_clock_time_back_to_blank(app, client, venue):
    """A blank time input clears that side back to not-clocked — e.g. an
    accidental early clock-in that should never have been recorded."""
    person_id, _m, _e = create_active_staff(app, venue["id"], name="ClearClockTime")
    today = date.today().isoformat()
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id, today),
        ).lastrowid
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at) VALUES (?, ?)",
            (shift_id, f"{today} 08:55:00"),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/attendance",
        data={"clock_in_time": "", "clock_out_time": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT clock_in_at, clock_out_at FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert row["clock_in_at"] is None
        assert row["clock_out_at"] is None


def test_cell_panel_has_editable_clock_time_inputs(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="EditableClockTest")
    today = date.today().isoformat()
    shift_id = _shift_no_attendance(app, venue["id"], person_id, today)

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/rota/cell/{person_id}/{today}")
    assert resp.status_code == 200
    assert f'action="/v/{venue["slug"]}/rota/shift/{shift_id}/attendance"'.encode() in resp.data
    assert b"Save clock times" in resp.data
