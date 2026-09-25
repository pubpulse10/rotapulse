"""
The venue owner's app_admin tier: where it comes from, and what must never
take it away.

Reported 25 September 2026 as "the Edit screen won't let me change a staff
member's hourly pay rate" — found after the first payroll run, with a rate
entered wrongly at onboarding and no way to correct it. The pay-rate field is
app_admin-only and always has been (spec §2.1/§4), so the field vanishing is
what losing app_admin LOOKS like: every other admin screen still works on
rota_admin, and nothing anywhere says you are no longer the owner.

The one thing in the codebase that could remove it: app/internal.py's Hub
access push. `_link_or_create_person` matched a Hub person to a local record
by email, the owner's own person row carries the landlord's email, and the
grant write then deleted every RotaPulse app_access row on that membership and
inserted a single one at the Hub's level.
"""

from app import db as db_module
from tests.conftest import create_active_staff, login_as_pub


def _headers():
    return {"Authorization": "Bearer test-secret"}


def _levels(app, membership_id):
    with app.app_context():
        conn = db_module.get_db()
        return {
            r["permission_level"]
            for r in conn.execute(
                """SELECT permission_level FROM app_access
                   WHERE venue_membership_id = ? AND status = 'active'""",
                (membership_id,),
            ).fetchall()
        }


def _internal_secret(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")


# --------------------------------------------------------------------------
# The Hub push must not touch the owner
# --------------------------------------------------------------------------


def test_a_hub_person_sharing_the_owners_email_does_not_adopt_the_owner(app, client, venue, monkeypatch):
    """The exact path that costs the owner app_admin: a landlord who invites
    themselves (or a spouse on the same address) through the Hub's People page."""
    _internal_secret(monkeypatch)

    resp = client.post(
        "/internal/access",
        json={"pub_id": venue["pub_id"], "person_id": 4242, "name": "Owner Person",
              "email": "owner@example.com", "level": "manager", "status": "active"},
        headers=_headers(),
    )
    assert resp.status_code == 200

    assert _levels(app, venue["owner_membership_id"]) == {"app_admin", "rota_admin"}
    with app.app_context():
        conn = db_module.get_db()
        owner = conn.execute(
            "SELECT * FROM person WHERE id = ?", (venue["owner_person_id"],)
        ).fetchone()
        assert owner["hub_person_id"] is None
        assert owner["pub_id"] == venue["pub_id"]


def test_a_push_for_a_person_row_already_attached_to_the_owner_is_ignored(app, client, venue, monkeypatch):
    """Repair case: an earlier version already stamped the Hub id onto the
    owner's row, so the email guard alone can't catch it on the next push."""
    _internal_secret(monkeypatch)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE person SET hub_person_id = 4242 WHERE id = ?",
                     (venue["owner_person_id"],))
        conn.commit()

    client.post(
        "/internal/access",
        json={"pub_id": venue["pub_id"], "person_id": 4242, "name": "Something Else",
              "email": "different@example.com", "level": "staff", "status": "active"},
        headers=_headers(),
    )

    assert _levels(app, venue["owner_membership_id"]) == {"app_admin", "rota_admin"}
    with app.app_context():
        conn = db_module.get_db()
        owner = conn.execute("SELECT * FROM person WHERE id = ?",
                             (venue["owner_person_id"],)).fetchone()
        assert owner["name"] == "Owner Person"      # not renamed from the Hub
        assert owner["email"] == "owner@example.com"


def test_the_push_still_works_normally_for_real_staff(app, client, venue, monkeypatch):
    """None of the above may blunt the feature itself."""
    _internal_secret(monkeypatch)

    client.post(
        "/internal/access",
        json={"pub_id": venue["pub_id"], "person_id": 77, "name": "Hub Manager",
              "email": "hubmanager@example.com", "level": "manager", "status": "active"},
        headers=_headers(),
    )

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            """SELECT app_access.permission_level, app_access.status
               FROM app_access
               JOIN venue_membership ON venue_membership.id = app_access.venue_membership_id
               JOIN person ON person.id = venue_membership.person_id
               WHERE person.hub_person_id = 77""",
        ).fetchone()
    assert row["permission_level"] == "rota_admin"
    assert row["status"] == "active"


def test_a_push_never_deletes_an_app_admin_row(app, client, venue, monkeypatch):
    """Belt and braces on the destructive statement itself, wherever the
    membership came from."""
    _internal_secret(monkeypatch)
    person_id, membership_id, email = create_active_staff(app, venue["id"], name="Odd One")
    with app.app_context():
        conn = db_module.get_db()
        app_id = db_module.get_app_id(conn, "rotapulse")
        conn.execute(
            """INSERT INTO app_access (venue_membership_id, app_id, permission_level, status)
               VALUES (?, ?, 'app_admin', 'active')""",
            (membership_id, app_id),
        )
        conn.execute("UPDATE person SET hub_person_id = 88 WHERE id = ?", (person_id,))
        conn.commit()

    client.post(
        "/internal/access",
        json={"pub_id": venue["pub_id"], "person_id": 88, "name": "Odd One",
              "email": email, "level": "staff", "status": "active"},
        headers=_headers(),
    )

    assert "app_admin" in _levels(app, membership_id)


# --------------------------------------------------------------------------
# And if it already happened, the owner's next page view repairs it
# --------------------------------------------------------------------------


def _strip_owner_app_admin(app, venue):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "DELETE FROM app_access WHERE venue_membership_id = ? AND permission_level = 'app_admin'",
            (venue["owner_membership_id"],),
        )
        conn.commit()


def test_the_owner_gets_app_admin_back_on_their_next_visit(app, client, venue):
    _strip_owner_app_admin(app, venue)
    assert _levels(app, venue["owner_membership_id"]) == {"rota_admin"}

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/admin/staff")

    assert resp.status_code == 200
    assert _levels(app, venue["owner_membership_id"]) == {"app_admin", "rota_admin"}


def test_and_can_then_edit_a_pay_rate_again(app, client, venue):
    """The reported symptom, end to end."""
    _strip_owner_app_admin(app, venue)
    _person_id, membership_id, _email = create_active_staff(app, venue["id"], name="Wrong Rate")
    login_as_pub(client, venue["pub_id"])

    page = client.get(f"/v/{venue['slug']}/admin/staff/{membership_id}/edit")
    assert b'name="hourly_pay_rate"' in page.data

    client.post(
        f"/v/{venue['slug']}/admin/staff/{membership_id}/edit",
        data={"name": "Wrong Rate", "hourly_pay_rate": "12.50"},
        follow_redirects=True,
    )

    with app.app_context():
        conn = db_module.get_db()
        detail = conn.execute(
            "SELECT hourly_pay_rate FROM rota_staff_detail WHERE venue_membership_id = ?",
            (membership_id,),
        ).fetchone()
    assert detail["hourly_pay_rate"] == 12.5


def test_a_revoked_owner_row_is_reactivated_too(app, client, venue):
    """Same repair whether the row was deleted or left revoked."""
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE app_access SET status = 'revoked' WHERE venue_membership_id = ? "
            "AND permission_level = 'app_admin'",
            (venue["owner_membership_id"],),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    client.get(f"/v/{venue['slug']}/admin/staff")

    assert "app_admin" in _levels(app, venue["owner_membership_id"])


def test_a_rota_admin_is_not_promoted_by_any_of_this(app, client, venue):
    """The repair is for whoever the pub_id cookie proves is the owner. An
    invited rota_admin must stay exactly where they are."""
    from tests.conftest import login_as_person

    person_id, membership_id, _email = create_active_staff(
        app, venue["id"], name="Delegated Admin", permission_level="rota_admin"
    )
    login_as_person(client, person_id)

    client.get(f"/v/{venue['slug']}/admin/staff")

    assert _levels(app, membership_id) == {"rota_admin"}


def test_a_staff_member_is_not_promoted_either(app, client, venue):
    from tests.conftest import login_as_person

    person_id, membership_id, _email = create_active_staff(app, venue["id"], name="Just Staff")
    login_as_person(client, person_id)

    client.get(f"/v/{venue['slug']}/")

    assert _levels(app, membership_id) == {"staff"}


# --------------------------------------------------------------------------
# What a rota_admin sees on the Edit screen
# --------------------------------------------------------------------------


def test_a_rota_admin_sees_the_pay_rate_and_who_can_change_it(app, client, venue):
    """It used to be absent altogether, which reads as "this app can't edit
    pay rates" rather than "you can't"."""
    from tests.conftest import login_as_person

    _p, membership_id, _e = create_active_staff(app, venue["id"], name="Paid Person")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_staff_detail SET hourly_pay_rate = 11.5 WHERE venue_membership_id = ?",
            (membership_id,),
        )
        conn.commit()
    admin_person_id, _m, _e = create_active_staff(
        app, venue["id"], name="Delegated Admin", permission_level="rota_admin"
    )
    login_as_person(client, admin_person_id)

    resp = client.get(f"/v/{venue['slug']}/admin/staff/{membership_id}/edit")

    assert b"11.50" in resp.data
    assert b"only the account owner can change pay rates" in resp.data
    assert b'name="hourly_pay_rate"' not in resp.data   # still not editable


def test_a_rota_admin_saving_the_form_leaves_the_pay_rate_alone(app, client, venue):
    """The form they post has no pay-rate field at all, and an absent field
    used to arrive as 0."""
    from tests.conftest import login_as_person

    _p, membership_id, _e = create_active_staff(app, venue["id"], name="Paid Person")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_staff_detail SET hourly_pay_rate = 11.5 WHERE venue_membership_id = ?",
            (membership_id,),
        )
        conn.commit()
    admin_person_id, _m, _e = create_active_staff(
        app, venue["id"], name="Delegated Admin", permission_level="rota_admin"
    )
    login_as_person(client, admin_person_id)

    client.post(
        f"/v/{venue['slug']}/admin/staff/{membership_id}/edit",
        data={"name": "Paid Person", "home_address": "2 New Street"},
        follow_redirects=True,
    )

    with app.app_context():
        conn = db_module.get_db()
        detail = conn.execute(
            "SELECT hourly_pay_rate, home_address FROM rota_staff_detail WHERE venue_membership_id = ?",
            (membership_id,),
        ).fetchone()
    assert detail["hourly_pay_rate"] == 11.5
    assert detail["home_address"] == "2 New Street"
