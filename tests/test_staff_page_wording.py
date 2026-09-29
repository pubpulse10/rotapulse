"""
The Staff page speaks the nav chip's language, and only offers what the
person looking at it can actually do (29 September 2026).

* The Permission column printed the stored levels as they are -- app_admin,
  rota_admin, staff -- while the chip said Owner / Manager / Staff.
* "Erase data" was shown to managers, who were then bounced to a login page
  because erasing is owner-only.
* The chip told a manager they had "full use of this app", which isn't true
  of RotaPulse: Settings, pay rates, permissions and erasing are owner-only.
"""

from app import db as db_module
from tests.conftest import create_active_staff, login_as_person, login_as_pub


def _mark_left(app, membership_id):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE venue_membership SET status = 'left' WHERE id = ?", (membership_id,))
        conn.commit()


def test_permission_column_says_owner_manager_staff(app, client, venue):
    create_active_staff(app, venue["id"], name="Mandy Manager", permission_level="rota_admin")
    create_active_staff(app, venue["id"], name="Sam Staff")
    login_as_pub(client, venue["pub_id"])

    body = client.get(f"/v/{venue['slug']}/admin/staff").get_data(as_text=True)

    assert "<td>Manager</td>" in body
    assert "<td>Staff</td>" in body
    assert "<td>Owner</td>" in body
    for raw in ("<td>app_admin</td>", "<td>rota_admin</td>", "<td>staff</td>", "Rota admin"):
        assert raw not in body


def test_the_owner_is_listed_once_not_once_per_grant(app, client, venue):
    """venues.setup() gives the owner two rows (app_admin + rota_admin)."""
    login_as_pub(client, venue["pub_id"])
    body = client.get(f"/v/{venue['slug']}/admin/staff").get_data(as_text=True)
    assert body.count("<td>Owner</td>") == 1
    assert "<td>Manager</td>" not in body


def test_erase_data_is_offered_to_the_owner(app, client, venue):
    _p, membership_id, _e = create_active_staff(app, venue["id"], name="Long Gone")
    _mark_left(app, membership_id)
    login_as_pub(client, venue["pub_id"])
    assert b"Erase data" in client.get(f"/v/{venue['slug']}/admin/staff").data


def test_erase_data_is_hidden_from_a_manager(app, client, venue):
    _p, membership_id, _e = create_active_staff(app, venue["id"], name="Long Gone")
    _mark_left(app, membership_id)
    manager_id, _m, _e2 = create_active_staff(app, venue["id"], name="Mandy Manager",
                                              permission_level="rota_admin")
    login_as_person(client, manager_id)
    resp = client.get(f"/v/{venue['slug']}/admin/staff")
    assert resp.status_code == 200
    assert b"Erase data" not in resp.data
    assert b"Reinstate" in resp.data


def test_the_managers_chip_says_what_a_rotapulse_manager_cannot_do(app, client, venue):
    manager_id, _m, _e = create_active_staff(app, venue["id"], name="Mandy Manager",
                                             permission_level="rota_admin")
    login_as_person(client, manager_id)
    body = client.get(f"/v/{venue['slug']}/staff/").get_data(as_text=True)
    assert "Full use of this app" not in body
    assert "Only the owner can open Settings" in body


def test_new_staff_and_clock_in_approvals_are_named_apart(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    staff_page = client.get(f"/v/{venue['slug']}/admin/staff").get_data(as_text=True)
    assert "New staff awaiting approval" in staff_page
    assert "View pending approvals" not in staff_page

    rota_page = client.get(f"/v/{venue['slug']}/rota/").get_data(as_text=True)
    assert "Clock-in approvals" in rota_page
