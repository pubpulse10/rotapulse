from datetime import date

from app import db as db_module
from tests.conftest import create_active_staff, login_as_person, login_as_pub


def _create_shift_for(app, venue_id, person_id):
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, ?, ?, 'scheduled')",
            (venue_id, person_id, date.today().isoformat(), "09:00", "17:00"),
        )
        conn.commit()
        return cur.lastrowid


def test_shift_detail_offers_a_dropdown_of_other_active_staff(app, client, venue):
    """Real request, 2026-08-18: staff previously had to know a colleague's
    numeric person ID to request a swap. Now shift_detail should list
    every other active staff member at the venue as a <select> option,
    and exclude both the shift's own owner and any inactive/left staff."""
    from_id, _m1, _e1 = create_active_staff(app, venue["id"], name="From Person")
    to_id, _m2, _e2 = create_active_staff(app, venue["id"], name="Zoe Colleague")
    left_id, left_membership_id, _e3 = create_active_staff(app, venue["id"], name="Left Person")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE venue_membership SET status = 'left' WHERE id = ?", (left_membership_id,))
        conn.commit()
    shift_id = _create_shift_for(app, venue["id"], from_id)

    login_as_person(client, from_id)
    resp = client.get(f"/v/{venue['slug']}/staff/shift/{shift_id}")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Zoe Colleague" in body
    assert "From Person" not in body.split("Request a swap")[1]
    assert "Left Person" not in body


def test_swap_requires_peer_then_admin_approval(app, client, venue):
    from_id, _m1, _e1 = create_active_staff(app, venue["id"], name="From")
    to_id, _m2, _e2 = create_active_staff(app, venue["id"], name="To")
    shift_id = _create_shift_for(app, venue["id"], from_id)

    login_as_person(client, from_id)
    client.post(f"/v/{venue['slug']}/staff/shift/{shift_id}/swap/request", data={"to_person_id": to_id})

    with app.app_context():
        conn = db_module.get_db()
        swap = conn.execute("SELECT * FROM shift_swap_request WHERE shift_id = ?", (shift_id,)).fetchone()
        assert swap["status"] == "pending_peer"
        swap_id = swap["id"]

    # Shift must still belong to the original person until fully approved.
    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT person_id FROM shift WHERE id = ?", (shift_id,)).fetchone()["person_id"] == from_id

    login_as_person(client, to_id)
    client.post(f"/v/{venue['slug']}/staff/swap/{swap_id}/accept")

    with app.app_context():
        conn = db_module.get_db()
        swap = conn.execute("SELECT status FROM shift_swap_request WHERE id = ?", (swap_id,)).fetchone()
        assert swap["status"] == "pending_admin"
        # Peer acceptance alone must not move the shift yet.
        assert conn.execute("SELECT person_id FROM shift WHERE id = ?", (shift_id,)).fetchone()["person_id"] == from_id

    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/rota/swap/{swap_id}/approve")

    with app.app_context():
        conn = db_module.get_db()
        swap = conn.execute("SELECT status FROM shift_swap_request WHERE id = ?", (swap_id,)).fetchone()
        assert swap["status"] == "approved"
        assert conn.execute("SELECT person_id FROM shift WHERE id = ?", (shift_id,)).fetchone()["person_id"] == to_id


def test_requesting_a_swap_messages_the_colleague(app, client, venue, monkeypatch):
    """Real report, 2026-10-29: the manager never got a swap to approve.
    The admin message only fires once the colleague accepts, and the
    colleague was never told a swap had been asked of them."""
    from_id, _m1, _e1 = create_active_staff(app, venue["id"], name="From")
    to_id, _m2, _e2 = create_active_staff(app, venue["id"], name="To")
    shift_id = _create_shift_for(app, venue["id"], from_id)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE person SET mobile = '07700900123' WHERE id = ?", (to_id,))
        conn.execute(
            """UPDATE app_access SET invite_method = 'sms' WHERE venue_membership_id IN
               (SELECT id FROM venue_membership WHERE person_id = ?)""",
            (to_id,),
        )
        conn.commit()

    sent = []
    monkeypatch.setattr("app.staff_portal.send_sms", lambda to, body: sent.append((to, body)) or True)

    login_as_person(client, from_id)
    client.post(f"/v/{venue['slug']}/staff/shift/{shift_id}/swap/request", data={"to_person_id": to_id})

    assert len(sent) == 1
    to, body = sent[0]
    assert to == "07700900123"
    assert "From has asked you to take" in body
    assert f"/v/{venue['slug']}/staff/swaps" in body
    assert body.isascii()
