"""
Only the owner decides who is a manager. edit_staff enforced that; create_staff
did not, so a manager could invite a second manager and approve the account
themselves (found 2026-10-07).
"""

from app import db as db_module
from tests.conftest import TEST_PUB_ID, create_active_staff, login_as_person, login_as_pub


def _create(client, venue, level):
    return client.post(f"/v/{venue['slug']}/admin/staff/create", data={
        "name": "New Person", "email": "new@example.com", "role_id": venue["role_id"],
        "invite_method": "email", "permission_level": level})


def _people_named(app, name):
    with app.app_context():
        return db_module.get_db().execute(
            "SELECT COUNT(*) AS n FROM person WHERE name = ?", (name,)).fetchone()["n"]


def test_a_manager_cannot_create_a_manager(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Manny Manager",
                                            permission_level="rota_admin")
    login_as_person(client, person_id)

    assert _create(client, venue, "rota_admin").status_code == 403
    assert _people_named(app, "New Person") == 0
    assert b'value="rota_admin"' not in client.get(f"/v/{venue['slug']}/admin/staff").data


def test_a_manager_can_still_create_staff(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Manny Manager",
                                            permission_level="rota_admin")
    login_as_person(client, person_id)

    assert _create(client, venue, "staff").status_code == 302
    assert _people_named(app, "New Person") == 1


def test_the_owner_can_create_a_manager(app, client, venue):
    login_as_pub(client, TEST_PUB_ID)

    assert _create(client, venue, "rota_admin").status_code == 302
    assert _people_named(app, "New Person") == 1
