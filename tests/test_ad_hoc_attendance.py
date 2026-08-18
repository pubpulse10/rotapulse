"""Ad-hoc/unplanned clock-in with admin approval, and the early-start
approval check on a REAL rostered shift — both added 2026-08-18. See the
module comment above app/rota_grid.py's approvals routes for the design.
"""

from datetime import date, datetime, timedelta

from app import db as db_module
from tests.conftest import create_active_staff, login_as_person, login_as_pub


def _create_shift_for(app, venue_id, person_id, shift_date, start_time="17:00", end_time="23:00"):
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, ?, ?, 'scheduled')",
            (venue_id, person_id, shift_date, start_time, end_time),
        )
        conn.commit()
        return cur.lastrowid


# ---------- Case A: fully ad-hoc, no shift at all today ----------


def test_starting_an_unplanned_shift_creates_an_ad_hoc_shift_pending_approval(app, client, venue, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.staff_portal.notify_admins",
        lambda db, venue, kind, subject, body: sent.append((kind, subject, body)),
    )
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Ad Hoc Alex")
    login_as_person(client, person_id)

    resp = client.post(f"/v/{venue['slug']}/staff/shift/ad-hoc/clock-in", data={}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"admin approval" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        shift = conn.execute("SELECT * FROM shift WHERE person_id = ?", (person_id,)).fetchone()
        assert shift["origin"] == "ad_hoc"
        assert shift["shift_date"] == date.today().isoformat()
        attendance = conn.execute("SELECT * FROM attendance WHERE shift_id = ?", (shift["id"],)).fetchone()
        assert attendance["approval_status"] == "pending"
        assert attendance["clock_in_at"] is not None

    assert len(sent) == 1
    assert sent[0][0] == "ad_hoc_shift"


def test_cannot_start_a_second_ad_hoc_shift_while_one_is_already_open(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Double Clock")
    login_as_person(client, person_id)

    client.post(f"/v/{venue['slug']}/staff/shift/ad-hoc/clock-in", data={})
    resp = client.post(f"/v/{venue['slug']}/staff/shift/ad-hoc/clock-in", data={}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"already have a shift today" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        count = conn.execute("SELECT COUNT(*) AS n FROM shift WHERE person_id = ?", (person_id,)).fetchone()["n"]
        assert count == 1


def test_ad_hoc_shift_end_time_updates_to_actual_clock_out(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Ad Hoc End Time")
    login_as_person(client, person_id)
    client.post(f"/v/{venue['slug']}/staff/shift/ad-hoc/clock-in", data={})

    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute("SELECT id FROM shift WHERE person_id = ?", (person_id,)).fetchone()["id"]

    client.post(f"/v/{venue['slug']}/staff/shift/{shift_id}/clock-out", data={})

    with app.app_context():
        conn = db_module.get_db()
        shift = conn.execute("SELECT * FROM shift WHERE id = ?", (shift_id,)).fetchone()
        attendance = conn.execute("SELECT * FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert shift["end_time"] == attendance["clock_out_at"][11:16]


def test_ad_hoc_button_hidden_once_already_clocked_in_today(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Already Working")
    login_as_person(client, person_id)
    client.post(f"/v/{venue['slug']}/staff/shift/ad-hoc/clock-in", data={})

    resp = client.get(f"/v/{venue['slug']}/staff/")
    assert b"Start an unplanned shift" not in resp.data


# ---------- Case B: a real rostered shift, started well ahead of plan ----------


def test_clocking_in_within_the_grace_window_needs_no_approval(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Slightly Early")
    today = date.today().isoformat()
    # Rostered to start in 20 minutes — inside the 30-minute grace window.
    start = (datetime.now() + timedelta(minutes=20)).strftime("%H:%M")
    shift_id = _create_shift_for(app, venue["id"], person_id, today, start_time=start)
    login_as_person(client, person_id)

    resp = client.post(f"/v/{venue['slug']}/staff/shift/{shift_id}/clock-in", data={}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"admin approval" not in resp.data

    with app.app_context():
        conn = db_module.get_db()
        attendance = conn.execute("SELECT * FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert attendance["approval_status"] is None


def test_clocking_in_well_ahead_of_a_real_shift_needs_approval(app, client, venue, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.staff_portal.notify_admins",
        lambda db, venue, kind, subject, body: sent.append((kind, subject, body)),
    )
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Very Early")
    today = date.today().isoformat()
    # Rostered to start in 2 hours — well outside the 30-minute grace window.
    start = (datetime.now() + timedelta(hours=2)).strftime("%H:%M")
    shift_id = _create_shift_for(app, venue["id"], person_id, today, start_time=start)
    login_as_person(client, person_id)

    resp = client.post(f"/v/{venue['slug']}/staff/shift/{shift_id}/clock-in", data={}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"admin approval" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        shift = conn.execute("SELECT * FROM shift WHERE id = ?", (shift_id,)).fetchone()
        # Uses the SAME shift row — no ad-hoc row was fabricated for a real,
        # already-rostered shift.
        assert shift["origin"] == "planned"
        attendance = conn.execute("SELECT * FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert attendance["approval_status"] == "pending"

    assert len(sent) == 1
    assert sent[0][0] == "ad_hoc_shift"


# ---------- Admin approve/reject ----------


def test_admin_can_approve_a_pending_shift(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Pending Person")
    login_as_person(client, person_id)
    client.post(f"/v/{venue['slug']}/staff/shift/ad-hoc/clock-in", data={})
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute("SELECT id FROM shift WHERE person_id = ?", (person_id,)).fetchone()["id"]

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/rota/shift/{shift_id}/attendance/approve", follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        attendance = conn.execute("SELECT * FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert attendance["approval_status"] == "approved"
        assert attendance["approval_decided_at"] is not None


def test_admin_can_reject_a_pending_shift(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Rejected Person")
    login_as_person(client, person_id)
    client.post(f"/v/{venue['slug']}/staff/shift/ad-hoc/clock-in", data={})
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute("SELECT id FROM shift WHERE person_id = ?", (person_id,)).fetchone()["id"]

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/rota/shift/{shift_id}/attendance/reject", follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        attendance = conn.execute("SELECT * FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
        assert attendance["approval_status"] == "rejected"
        # Kept, not deleted — the audit trail of what was actually claimed.
        assert attendance["clock_in_at"] is not None


def test_approve_reject_is_scoped_to_the_caller_venue(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Cross Venue Person")
    login_as_person(client, person_id)
    client.post(f"/v/{venue['slug']}/staff/shift/ad-hoc/clock-in", data={})
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute("SELECT id FROM shift WHERE person_id = ?", (person_id,)).fetchone()["id"]

        # A second, fully separate venue+owner — same shape as the `venue`
        # fixture, mirroring the pattern used elsewhere for cross-venue
        # IDOR checks (see test_admin_staff_edit.py).
        other_pub_id = 999
        other_venue_id = conn.execute(
            "INSERT INTO venue (pub_id, name, slug) VALUES (?, 'Other Venue', 'othervenue')", (other_pub_id,)
        ).lastrowid
        conn.execute("INSERT INTO rota_subscription (venue_id, plan) VALUES (?, 'active')", (other_venue_id,))
        other_owner_id = conn.execute(
            "INSERT INTO person (name, pub_id) VALUES ('Other Owner', ?)", (other_pub_id,)
        ).lastrowid
        other_membership_id = conn.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')",
            (other_owner_id, other_venue_id),
        ).lastrowid
        app_id = db_module.get_app_id(conn, "rotapulse")
        for level in ("app_admin", "rota_admin"):
            conn.execute(
                """INSERT INTO app_access (venue_membership_id, app_id, permission_level, status, accepted_at, approved_at)
                   VALUES (?, ?, ?, 'active', datetime('now'), datetime('now'))""",
                (other_membership_id, app_id, level),
            )
        conn.commit()

    login_as_pub(client, other_pub_id)
    resp = client.post(f"/v/othervenue/rota/shift/{shift_id}/attendance/approve")
    assert resp.status_code == 404


def test_approvals_queue_lists_pending_and_not_decided(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Queue Person")
    login_as_person(client, person_id)
    client.post(f"/v/{venue['slug']}/staff/shift/ad-hoc/clock-in", data={})

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/rota/approvals")
    assert resp.status_code == 200
    assert b"Queue Person" in resp.data
    assert b"Unplanned" in resp.data


# ---------- Payroll: rejected excluded, pending still counted ----------


def test_rejected_attendance_is_excluded_from_payroll_but_pending_counts(app, client, venue):
    person_id, membership_id, _e = create_active_staff(app, venue["id"], name="Payroll Person")
    today = date.today().isoformat()

    # Ad-hoc shift #1 -> will be approved.
    login_as_person(client, person_id)
    client.post(f"/v/{venue['slug']}/staff/shift/ad-hoc/clock-in", data={})
    with app.app_context():
        conn = db_module.get_db()
        shift_1 = conn.execute("SELECT id FROM shift WHERE person_id = ?", (person_id,)).fetchone()["id"]
    client.post(f"/v/{venue['slug']}/staff/shift/{shift_1}/clock-out", data={})

    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/rota/shift/{shift_1}/attendance/reject")

    # Ad-hoc shift #2, same day -> stays pending (still counts in payroll).
    with app.app_context():
        conn = db_module.get_db()
        shift_2 = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status, origin) "
            "VALUES (?, ?, ?, '20:00', '22:00', 'scheduled', 'ad_hoc')",
            (venue["id"], person_id, today),
        ).lastrowid
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at, approval_status) "
            "VALUES (?, ?, ?, 'pending')",
            (shift_2, f"{today} 20:00:00", f"{today} 22:00:00"),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/payroll/?start={today}&end={today}")
    assert resp.status_code == 200
    assert b"Payroll Person" in resp.data
    assert b"still awaiting admin approval" in resp.data
    # 2 hours (shift #2) should be in the total, not 0 and not more (shift #1 rejected).
    assert b"2.0" in resp.data
