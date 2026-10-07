"""
"Mark as left" revokes someone's RotaPulse access. It must only ever touch a
membership of the venue it is called on: the access update is keyed on the
membership id alone, and it used to run even when the id belonged to another
venue, so one venue's manager could lock out every other venue's staff by
walking the ids (found 2026-10-07). Nor may it be used on the account owner,
who would have no way back in to the app or to its billing.
"""

from app import db as db_module
from tests.conftest import TEST_PUB_ID, create_active_staff, login_as_pub


def other_venue(app):
    with app.app_context():
        conn = db_module.get_db()
        venue_id = conn.execute(
            "INSERT INTO venue (pub_id, name, slug) VALUES (999, 'Other Venue', 'othervenue')"
        ).lastrowid
        conn.execute("INSERT INTO rota_subscription (venue_id, plan) VALUES (?, 'active')", (venue_id,))
        conn.commit()
    return venue_id


def access(app, membership_id):
    with app.app_context():
        conn = db_module.get_db()
        membership = conn.execute(
            "SELECT status FROM venue_membership WHERE id = ?", (membership_id,)).fetchone()["status"]
        levels = [r["status"] for r in conn.execute(
            "SELECT status FROM app_access WHERE venue_membership_id = ?", (membership_id,))]
    return membership, levels


def test_another_venues_staff_cannot_be_marked_as_left(client, app, venue):
    _, victim, _ = create_active_staff(app, other_venue(app), name="Victim Vera")
    login_as_pub(client, TEST_PUB_ID)

    resp = client.post(f"/v/{venue['slug']}/admin/staff/{victim}/leave")

    assert resp.status_code == 404
    assert access(app, victim) == ("active", ["active"])


def test_the_account_owner_cannot_be_marked_as_left(client, app, venue):
    _, manager, _ = create_active_staff(app, venue["id"], name="Manny Manager",
                                         permission_level="rota_admin")
    login_as_pub(client, TEST_PUB_ID)

    resp = client.post(f"/v/{venue['slug']}/admin/staff/{venue['owner_membership_id']}/leave",
                       follow_redirects=True)

    assert b"account owner can" in resp.data
    assert access(app, venue["owner_membership_id"]) == ("active", ["active", "active"])


def test_this_venues_own_staff_can_still_be_marked_as_left(client, app, venue):
    _, membership, _ = create_active_staff(app, venue["id"])
    login_as_pub(client, TEST_PUB_ID)

    client.post(f"/v/{venue['slug']}/admin/staff/{membership}/leave")

    assert access(app, membership) == ("left", ["revoked"])
