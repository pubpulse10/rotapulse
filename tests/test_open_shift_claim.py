from datetime import date

from app import db as db_module
from tests.conftest import create_active_staff, login_as_person, login_as_pub


def _create_open_shift(app, venue_id, role_id=None):
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO shift (venue_id, venue_role_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, ?, ?, 'open')",
            (venue_id, role_id, date.today().isoformat(), "18:00", "23:00"),
        )
        conn.commit()
        return cur.lastrowid


def test_first_claim_wins(app, client, venue):
    shift_id = _create_open_shift(app, venue["id"])
    person_id, _membership_id, _email = create_active_staff(app, venue["id"], name="Claimer")
    login_as_person(client, person_id)

    resp = client.post(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}/claim", follow_redirects=True)
    assert b"claimed" in resp.data.lower()

    with app.app_context():
        conn = db_module.get_db()
        shift_row = conn.execute("SELECT * FROM shift WHERE id = ?", (shift_id,)).fetchone()
        assert shift_row["status"] == "scheduled"
        assert shift_row["person_id"] == person_id


def test_claiming_an_open_shift_while_on_approved_leave_shows_the_shift_not_leave(app, client, venue):
    """Real report, 2026-08-27: 'Donna' claimed an open shift for a day she
    was down for approved leave -- the grid cell kept showing the leave
    state, hiding the shift she'd actually just picked up. claim_open_shift
    has no leave check at all (deliberately, matching create_shift -- covering
    an open shift is never blocked by a leave record), so the fix is on the
    display side: a real shift now always wins over a leave record in the
    grid's per-cell state."""
    today = date.today().isoformat()
    shift_id = _create_open_shift(app, venue["id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Donna")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO leave_request (person_id, venue_id, start_date, end_date, status) VALUES (?, ?, ?, ?, 'approved')",
            (person_id, venue["id"], today, today),
        )
        conn.commit()

    login_as_person(client, person_id)
    resp = client.post(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}/claim", follow_redirects=True)
    assert b"claimed" in resp.data.lower()

    login_as_pub(client, venue["pub_id"])
    grid_resp = client.get(f"/v/{venue['slug']}/rota/")
    assert b"cell-shift" in grid_resp.data
    assert b"18:00-23:00" in grid_resp.data
    assert b"cell-leave" not in grid_resp.data

    staff_resp = client.get(f"/v/{venue['slug']}/staff/rota")
    assert b"18:00-23:00" in staff_resp.data
    assert b"Hol" not in staff_resp.data


def test_second_claim_is_rejected(app, client, venue):
    shift_id = _create_open_shift(app, venue["id"])
    first_id, _m1, _e1 = create_active_staff(app, venue["id"], name="First")
    second_id, _m2, _e2 = create_active_staff(app, venue["id"], name="Second")

    login_as_person(client, first_id)
    client.post(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}/claim")

    login_as_person(client, second_id)
    resp = client.post(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}/claim", follow_redirects=True)
    assert b"already claimed" in resp.data.lower()

    with app.app_context():
        conn = db_module.get_db()
        shift_row = conn.execute("SELECT person_id FROM shift WHERE id = ?", (shift_id,)).fetchone()
        assert shift_row["person_id"] == first_id


def test_admin_can_open_a_scheduled_shift(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _membership_id, _email = create_active_staff(app, venue["id"], name="Staffer")
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, ?, ?, 'scheduled')",
            (venue["id"], person_id, date.today().isoformat(), "09:00", "17:00"),
        )
        conn.commit()
        shift_id = cur.lastrowid

    client.post(f"/v/{venue['slug']}/rota/shift/{shift_id}/open", follow_redirects=True)

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT status, person_id FROM shift WHERE id = ?", (shift_id,)).fetchone()
        assert row["status"] == "open"
        assert row["person_id"] is None


def test_admin_can_create_a_new_open_shift_directly(app, client, venue):
    """Regression coverage for the old two-step workaround (create a
    scheduled shift for someone, then immediately re-open it) — creating a
    shift with no person_id should go straight to 'open'."""
    login_as_pub(client, venue["pub_id"])
    today = date.today().isoformat()

    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/create",
        data={"shift_date": today, "start_time": "18:00", "end_time": "23:00"},  # no person_id at all
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT * FROM shift WHERE venue_id = ? AND shift_date = ? AND start_time = '18:00'",
            (venue["id"], today),
        ).fetchone()
        assert row is not None
        assert row["status"] == "open"
        assert row["person_id"] is None

    resp = client.get(f"/v/{venue['slug']}/rota/")
    assert b"18:00-23:00" in resp.data
    assert b"Add open shift" in resp.data


def test_create_shift_with_person_id_is_still_scheduled_not_open(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="StillScheduled")
    today = date.today().isoformat()

    client.post(
        f"/v/{venue['slug']}/rota/shift/create",
        data={"person_id": person_id, "shift_date": today, "start_time": "09:00", "end_time": "17:00"},
    )

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT * FROM shift WHERE person_id = ?", (person_id,)).fetchone()
        assert row["status"] == "scheduled"


def test_open_shift_appears_on_its_own_grid_row(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    shift_id = _create_open_shift(app, venue["id"])

    resp = client.get(f"/v/{venue['slug']}/rota/")
    assert resp.status_code == 200
    assert b'class="open-row"' in resp.data
    assert b"cell-openshift" in resp.data
    # The chip is a draggable .shift-chip now (2026-08-19), tagged with its
    # own shift id for the drag JS, and its tap target is the day-level
    # panel (not the single-shift one, which is no longer linked from the
    # grid but still reachable directly — see test_open_shift_panel_* below).
    assert f'data-shift-id="{shift_id}"'.encode() in resp.data
    assert b'class="shift-chip open-shift-chip"' in resp.data
    assert b"/rota/open-shift/day/" in resp.data


def test_open_shift_panel_shows_notify_and_delete(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    shift_id = _create_open_shift(app, venue["id"])

    resp = client.get(f"/v/{venue['slug']}/rota/open-shift/{shift_id}")
    assert resp.status_code == 200
    assert b"Notify staff" in resp.data
    assert b"Delete" in resp.data


def test_open_shift_panel_delete_removes_it_from_the_grid(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    shift_id = _create_open_shift(app, venue["id"])

    resp = client.post(f"/v/{venue['slug']}/rota/shift/{shift_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert b"cell-openshift" not in resp.data

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT 1 FROM shift WHERE id = ?", (shift_id,)).fetchone() is None


def test_open_shift_panel_404s_for_a_scheduled_shift(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="NotOpen")
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, ?, ?, 'scheduled')",
            (venue["id"], person_id, date.today().isoformat(), "09:00", "17:00"),
        )
        conn.commit()
        shift_id = cur.lastrowid

    resp = client.get(f"/v/{venue['slug']}/rota/open-shift/{shift_id}")
    assert resp.status_code == 404


def test_open_shift_panel_requires_admin_permission(client, venue):
    resp = client.get(f"/v/{venue['slug']}/rota/open-shift/1", follow_redirects=False)
    assert resp.status_code == 302


def test_delete_shift_after_notifying_it_does_not_violate_foreign_key(app, client, venue):
    """Regression test: deleting a shift that's already been through
    "Notify staff" (which logs a shift_open_notification row referencing
    it) used to raise sqlite3.IntegrityError, since delete_shift() never
    cleaned that row up first."""
    login_as_pub(client, venue["pub_id"])
    shift_id = _create_open_shift(app, venue["id"])

    client.post(f"/v/{venue['slug']}/rota/shift/{shift_id}/notify")
    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT 1 FROM shift_open_notification WHERE shift_id = ?", (shift_id,)).fetchone()

    resp = client.post(f"/v/{venue['slug']}/rota/shift/{shift_id}/delete", follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT 1 FROM shift WHERE id = ?", (shift_id,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM shift_open_notification WHERE shift_id = ?", (shift_id,)).fetchone() is None


def test_delete_shift_with_a_pending_swap_request_does_not_violate_foreign_key(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    from_person, _m1, _e1 = create_active_staff(app, venue["id"], name="SwapFrom")
    to_person, _m2, _e2 = create_active_staff(app, venue["id"], name="SwapTo")
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue["id"], from_person, date.today().isoformat()),
        ).lastrowid
        conn.execute(
            "INSERT INTO shift_swap_request (shift_id, from_person_id, to_person_id) VALUES (?, ?, ?)",
            (shift_id, from_person, to_person),
        )
        conn.commit()

    resp = client.post(f"/v/{venue['slug']}/rota/shift/{shift_id}/delete", follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT 1 FROM shift WHERE id = ?", (shift_id,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM shift_swap_request WHERE shift_id = ?", (shift_id,)).fetchone() is None
