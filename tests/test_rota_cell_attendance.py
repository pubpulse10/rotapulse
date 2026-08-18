from datetime import date

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
