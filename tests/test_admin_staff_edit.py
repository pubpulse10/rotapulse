from app import db as db_module
from tests.conftest import create_active_staff, login_as_pub


def test_edit_staff_page_shows_current_values(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, membership_id, email = create_active_staff(app, venue["id"], name="Edit Me")

    resp = client.get(f"/v/{venue['slug']}/admin/staff/{membership_id}/edit")
    assert resp.status_code == 200
    assert b"Edit Me" in resp.data
    assert email.encode() in resp.data


def test_admin_can_update_name_contact_dob_role_and_availability(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, membership_id, _email = create_active_staff(app, venue["id"], name="Original Name")

    resp = client.post(
        f"/v/{venue['slug']}/admin/staff/{membership_id}/edit",
        data={
            "name": "Updated Name",
            "email": "updated@example.com",
            "mobile": "07700900123",
            "date_of_birth": "1990-06-15",
            "role_id": str(venue["role_id"]),
            "hourly_pay_rate": "15.00",
            "home_address": "1 New Street",
            "available_mon": "on",
            "available_tue": "on",
            # wed-sun left unchecked
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"record updated" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        person = conn.execute("SELECT * FROM person WHERE id = ?", (person_id,)).fetchone()
        assert person["name"] == "Updated Name"
        assert person["email"] == "updated@example.com"
        assert person["mobile"] == "07700900123"
        assert person["date_of_birth"] == "1990-06-15"

        membership = conn.execute("SELECT * FROM venue_membership WHERE id = ?", (membership_id,)).fetchone()
        assert membership["job_role_id"] == venue["role_id"]

        detail = conn.execute("SELECT * FROM rota_staff_detail WHERE venue_membership_id = ?", (membership_id,)).fetchone()
        assert detail["hourly_pay_rate"] == 15.0
        assert detail["home_address"] == "1 New Street"
        import json
        availability = json.loads(detail["availability"])
        assert availability["mon"] is True
        assert availability["tue"] is True
        assert availability["wed"] is False


def test_rota_admin_cannot_change_pay_rate_through_edit_form(app, client, venue):
    """Pay rate stays app_admin-only (spec §2.1/§4) — a rota_admin must not
    be able to smuggle a change through the consolidated edit form even by
    posting the field directly."""
    login_as_pub(client, venue["pub_id"])
    person_id, membership_id, _email = create_active_staff(app, venue["id"], name="RateTest")

    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_staff_detail SET hourly_pay_rate = 10 WHERE venue_membership_id = ?", (membership_id,)
        )
        conn.commit()

    rota_admin_id, rota_admin_membership_id, rota_admin_email = create_active_staff(
        app, venue["id"], name="RotaAdminUser", permission_level="rota_admin"
    )
    from tests.conftest import login_as_person

    login_as_person(client, rota_admin_id)
    client.post(
        f"/v/{venue['slug']}/admin/staff/{membership_id}/edit",
        data={"name": "RateTest", "hourly_pay_rate": "999.00"},
    )

    with app.app_context():
        conn = db_module.get_db()
        detail = conn.execute("SELECT hourly_pay_rate FROM rota_staff_detail WHERE venue_membership_id = ?", (membership_id,)).fetchone()
        assert detail["hourly_pay_rate"] == 10  # unchanged, not 999


def test_admin_can_change_permission_level(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, membership_id, _email = create_active_staff(
        app, venue["id"], name="Promote Me", permission_level="staff"
    )

    client.post(
        f"/v/{venue['slug']}/admin/staff/{membership_id}/edit",
        data={"name": "Promote Me", "permission_level": "rota_admin"},
    )

    with app.app_context():
        conn = db_module.get_db()
        access = conn.execute(
            """SELECT permission_level FROM app_access
               WHERE venue_membership_id = ? AND app_id = (SELECT id FROM app WHERE key = 'rotapulse')""",
            (membership_id,),
        ).fetchone()
    assert access["permission_level"] == "rota_admin"


def test_rota_admin_cannot_change_permission_level_through_edit_form(app, client, venue):
    """Same protection as pay rate — permission level is app_admin-only, a
    rota_admin editing someone must not be able to grant rota_admin (or
    revoke it) by posting the field directly."""
    login_as_pub(client, venue["pub_id"])
    person_id, membership_id, _email = create_active_staff(
        app, venue["id"], name="StayPut", permission_level="staff"
    )

    rota_admin_id, _rota_admin_membership_id, _rota_admin_email = create_active_staff(
        app, venue["id"], name="RotaAdminUser2", permission_level="rota_admin"
    )
    from tests.conftest import login_as_person

    login_as_person(client, rota_admin_id)
    client.post(
        f"/v/{venue['slug']}/admin/staff/{membership_id}/edit",
        data={"name": "StayPut", "permission_level": "rota_admin"},
    )

    with app.app_context():
        conn = db_module.get_db()
        access = conn.execute(
            """SELECT permission_level FROM app_access
               WHERE venue_membership_id = ? AND app_id = (SELECT id FROM app WHERE key = 'rotapulse')""",
            (membership_id,),
        ).fetchone()
    assert access["permission_level"] == "staff"  # unchanged


def test_permission_level_cannot_be_smuggled_to_app_admin(app, client, venue):
    """app_admin is the owner-only SSO tier — must never be grantable
    through this form even by an app_admin posting the field directly."""
    login_as_pub(client, venue["pub_id"])
    person_id, membership_id, _email = create_active_staff(
        app, venue["id"], name="NoEscalation", permission_level="staff"
    )

    client.post(
        f"/v/{venue['slug']}/admin/staff/{membership_id}/edit",
        data={"name": "NoEscalation", "permission_level": "app_admin"},
    )

    with app.app_context():
        conn = db_module.get_db()
        access = conn.execute(
            """SELECT permission_level FROM app_access
               WHERE venue_membership_id = ? AND app_id = (SELECT id FROM app WHERE key = 'rotapulse')""",
            (membership_id,),
        ).fetchone()
    assert access["permission_level"] == "staff"  # unchanged, not app_admin


def test_editing_owner_with_dual_access_rows_does_not_crash(app, client, venue):
    """The venue owner has TWO app_access rows on one membership (app_admin
    + rota_admin) — a fundamentally different shape from regular invited
    staff's single row. The permission field must not appear (there's no
    single unambiguous level to show/edit), and the page must not crash."""
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/admin/staff/{venue['owner_membership_id']}/edit")
    assert resp.status_code == 200
    assert b'name="permission_level"' not in resp.data

    post_resp = client.post(
        f"/v/{venue['slug']}/admin/staff/{venue['owner_membership_id']}/edit",
        data={"name": "Owner Person", "permission_level": "staff"},
    )
    assert post_resp.status_code == 302  # saved fine, no crash

    with app.app_context():
        conn = db_module.get_db()
        levels = {
            row["permission_level"]
            for row in conn.execute(
                """SELECT permission_level FROM app_access
                   WHERE venue_membership_id = ? AND app_id = (SELECT id FROM app WHERE key = 'rotapulse')""",
                (venue["owner_membership_id"],),
            ).fetchall()
        }
    assert levels == {"app_admin", "rota_admin"}  # both untouched


def test_edit_staff_requires_name(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, membership_id, _email = create_active_staff(app, venue["id"], name="Keep This Name")

    resp = client.post(
        f"/v/{venue['slug']}/admin/staff/{membership_id}/edit",
        data={"name": ""},
        follow_redirects=True,
    )
    assert b"required" in resp.data.lower()

    with app.app_context():
        conn = db_module.get_db()
        person = conn.execute("SELECT name FROM person WHERE id = ?", (person_id,)).fetchone()
        assert person["name"] == "Keep This Name"


def test_edit_staff_404s_for_membership_at_another_venue(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/admin/staff/999999/edit")
    assert resp.status_code == 404


def test_edit_staff_requires_admin_permission(client, venue):
    resp = client.get(f"/v/{venue['slug']}/admin/staff/1/edit", follow_redirects=False)
    assert resp.status_code == 302


def test_staff_list_shows_one_clear_status_not_two_raw_fields(app, client, venue):
    """Regression coverage for the confusing "active / active" display —
    the list should show one collapsed label, and the raw membership_status
    just being 'active' must not be exposed as a second copy of the word."""
    login_as_pub(client, venue["pub_id"])
    create_active_staff(app, venue["id"], name="StatusCheck")

    resp = client.get(f"/v/{venue['slug']}/admin/staff")
    assert resp.status_code == 200
    assert b"Active" in resp.data
    assert b"active / active" not in resp.data.lower()


def test_staff_list_shows_left_once_membership_status_is_left(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    _person_id, membership_id, _email = create_active_staff(app, venue["id"], name="LeftCheck")

    client.post(f"/v/{venue['slug']}/admin/staff/{membership_id}/leave")

    resp = client.get(f"/v/{venue['slug']}/admin/staff")
    text = resp.data.decode()
    row_start = text.index("LeftCheck")
    row = text[row_start : row_start + 400]
    assert "Left" in row
    assert "revoked" not in row.lower()  # the underlying access_status isn't shown raw in the table cell
