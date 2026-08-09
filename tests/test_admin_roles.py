from app import db as db_module
from tests.conftest import create_active_staff, login_as_pub


def test_roles_page_lists_existing_roles(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/admin/roles")
    assert resp.status_code == 200
    assert b"Bar staff" in resp.data


def test_venue_settings_links_to_roles(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/admin/settings")
    assert f"/v/{venue['slug']}/admin/roles".encode() in resp.data


def test_create_role(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/admin/roles/create", data={"name": "Kitchen"})
    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT * FROM venue_role WHERE venue_id = ? AND name = 'Kitchen'", (venue["id"],)
        ).fetchone()
    assert row is not None


def test_rename_role(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    client.post(
        f"/v/{venue['slug']}/admin/roles/{venue['role_id']}/rename", data={"name": "Front of house"}
    )
    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT name FROM venue_role WHERE id = ?", (venue["role_id"],)).fetchone()
    assert row["name"] == "Front of house"


def test_delete_unused_role_succeeds(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/admin/roles/create", data={"name": "Kitchen"})
    with app.app_context():
        conn = db_module.get_db()
        kitchen_id = conn.execute(
            "SELECT id FROM venue_role WHERE venue_id = ? AND name = 'Kitchen'", (venue["id"],)
        ).fetchone()["id"]

    resp = client.post(f"/v/{venue['slug']}/admin/roles/{kitchen_id}/delete", follow_redirects=True)
    assert b"Role deleted." in resp.data

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT * FROM venue_role WHERE id = ?", (kitchen_id,)).fetchone()
    assert row is None


def test_delete_role_still_assigned_to_staff_is_blocked_not_a_crash(app, client, venue):
    """Regression test: deleting a role with a foreign_keys=ON connection
    while staff still reference it via job_role_id used to raise an
    unhandled sqlite3.IntegrityError (500), reproduced live before this fix.
    Must now be blocked with a friendly message and no data loss."""
    create_active_staff(app, venue["id"], name="Alex Morgan", role_id=venue["role_id"])
    login_as_pub(client, venue["pub_id"])

    resp = client.post(
        f"/v/{venue['slug']}/admin/roles/{venue['role_id']}/delete", follow_redirects=True
    )
    assert resp.status_code == 200
    assert b"Can&#39;t delete this role" in resp.data or b"Can't delete this role" in resp.data
    assert b"1 staff member" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT * FROM venue_role WHERE id = ?", (venue["role_id"],)).fetchone()
    assert row is not None  # still exists, not deleted


def test_roles_require_app_admin_not_rota_admin(app, client, venue):
    """spec §5.2: role management is app_admin-only, unlike the day-to-day
    staff directory which rota_admin can also use. app_admin-only routes
    redirect to the shared PubPulse login rather than 403ing, since
    app_admin is only ever reachable via that owner-only SSO path (see
    require_permission's single-level-app_admin branch)."""
    _person_id, membership_id, email = create_active_staff(
        app, venue["id"], name="Rota Admin Only", permission_level="rota_admin"
    )
    from tests.conftest import login_as_person

    login_as_person(client, _person_id)
    resp = client.get(f"/v/{venue['slug']}/admin/roles")
    assert resp.status_code == 302
    assert b"Roles" not in resp.data


def test_invite_form_role_dropdown_offers_created_roles(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/admin/roles/create", data={"name": "Kitchen"})
    resp = client.get(f"/v/{venue['slug']}/admin/staff")
    assert b"Bar staff" in resp.data
    assert b"Kitchen" in resp.data


def test_staff_edit_form_can_reassign_role(app, client, venue):
    person_id, membership_id, email = create_active_staff(
        app, venue["id"], name="Jordan Lee", role_id=venue["role_id"]
    )
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/admin/roles/create", data={"name": "Kitchen"})
    with app.app_context():
        conn = db_module.get_db()
        kitchen_id = conn.execute(
            "SELECT id FROM venue_role WHERE venue_id = ? AND name = 'Kitchen'", (venue["id"],)
        ).fetchone()["id"]

    login_as_pub(client, venue["pub_id"])
    client.post(
        f"/v/{venue['slug']}/admin/staff/{membership_id}/edit",
        data={"name": "Jordan Lee", "role_id": str(kitchen_id)},
    )

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT job_role_id FROM venue_membership WHERE id = ?", (membership_id,)
        ).fetchone()
    assert row["job_role_id"] == kitchen_id
