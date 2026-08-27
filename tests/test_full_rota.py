from datetime import date

from app import db as db_module
from app.rota_grid import _monday_of
from tests.conftest import create_active_staff, login_as_person


def _shift(app, venue_id, person_id, shift_date, start="09:00", end="17:00"):
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, ?, ?, 'scheduled')",
            (venue_id, person_id, shift_date, start, end),
        )
        conn.commit()
        return cur.lastrowid


def _open_shift(app, venue_id, shift_date, start="18:00", end="23:00"):
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO shift (venue_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, ?, 'open')",
            (venue_id, shift_date, start, end),
        )
        conn.commit()
        return cur.lastrowid


def test_staff_can_see_a_colleagues_shift_on_the_full_rota(app, client, venue):
    """Real request, 2026-08-19: staff could only ever see their own shifts,
    with no way to know who else is on with them."""
    viewer_id, _m1, _e1 = create_active_staff(app, venue["id"], name="Viewer")
    colleague_id, _m2, _e2 = create_active_staff(app, venue["id"], name="Colleague")
    monday = _monday_of(date.today())
    _shift(app, venue["id"], colleague_id, (monday).isoformat())

    login_as_person(client, viewer_id)
    resp = client.get(f"/v/{venue['slug']}/staff/rota")
    assert resp.status_code == 200
    assert b"Colleague" in resp.data
    assert b"09:00-17:00" in resp.data


def test_full_rota_shows_open_shifts(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Viewer2")
    monday = _monday_of(date.today())
    _open_shift(app, venue["id"], monday.isoformat())

    login_as_person(client, person_id)
    resp = client.get(f"/v/{venue['slug']}/staff/rota")
    assert resp.status_code == 200
    assert b"18:00-23:00" in resp.data


def test_full_rota_has_no_admin_only_controls(app, client, venue):
    """Read-only: none of the admin week grid's actions (notify/copy/clear,
    drag-and-drop) should appear here."""
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Viewer3")

    login_as_person(client, person_id)
    resp = client.get(f"/v/{venue['slug']}/staff/rota")
    assert resp.status_code == 200
    assert b"Clear week" not in resp.data
    assert b"Copy Week" not in resp.data
    assert b"data-shift-id" not in resp.data


def test_full_rota_prev_next_week_links_navigate(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Viewer4")

    login_as_person(client, person_id)
    resp = client.get(f"/v/{venue['slug']}/staff/rota")
    assert resp.status_code == 200
    assert b"Prev week" in resp.data
    assert b"Next week" in resp.data
