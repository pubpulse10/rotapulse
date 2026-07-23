from app import db as db_module
from app.consent import erase_person_sensitive_data
from tests.conftest import create_active_staff, login_as_pub


def test_cannot_erase_an_active_staff_member(app, venue):
    person_id, membership_id, _email = create_active_staff(app, venue["id"])
    with app.app_context():
        assert erase_person_sensitive_data(venue["id"], membership_id) is False


def test_erase_clears_identity_but_keeps_shift_hours(app, venue):
    person_id, membership_id, _email = create_active_staff(app, venue["id"])
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE person SET avatar_url = 'somefile.jpg' WHERE id = ?", (person_id,))
        conn.execute("UPDATE venue_membership SET status = 'left' WHERE id = ?", (membership_id,))
        conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, '2026-01-01', '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id),
        )
        conn.commit()

        assert erase_person_sensitive_data(venue["id"], membership_id) is True

        person = conn.execute("SELECT * FROM person WHERE id = ?", (person_id,)).fetchone()
        assert person["email"] is None
        assert person["avatar_url"] is None
        assert person["erased_at"] is not None
        assert person["name"] is not None  # name retained for historic readability

        # Shift/hours records are untouched.
        shift_row = conn.execute("SELECT * FROM shift WHERE person_id = ?", (person_id,)).fetchone()
        assert shift_row is not None


def test_admin_erase_route_requires_left_status(app, client, venue):
    person_id, membership_id, _email = create_active_staff(app, venue["id"])
    login_as_pub(client, venue["pub_id"])

    resp = client.post(f"/v/{venue['slug']}/admin/staff/{membership_id}/erase", follow_redirects=True)
    assert b"already left" in resp.data.lower()
