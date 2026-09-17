from datetime import timedelta

from app import db as db_module
from app.uk_time import uk_today
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


# ---------- Retiring a role (2026-09-17) ----------
#
# A role that has ever been used can't be deleted at all. venue_membership,
# shift and leave_block_role each hold a foreign key to venue_role and the
# connection runs with foreign_keys=ON, so the DELETE raises. Two of those
# three had a friendly refusal in front of them; shift did not, and since
# old shifts are never tidied up, within a few weeks of real use that is
# every role — each one a 500 page waiting for the landlord who tries.
#
# Delete therefore only ever removes a role nothing has touched, and
# archiving retires the rest: gone from every form that picks a role, still
# named on everything that already used it. What's easy to get wrong:
#
# * Refusing on its own isn't an answer. Without a way to retire a role,
#   one typo is permanent and every dropdown carries it forever.
# * An archived role must NOT disappear from a record that already holds
#   it. Drop it from the <select> and the next save of an unrelated field
#   silently clears the role.
# * Adding a role back by name means the old one, history and all — not a
#   UNIQUE constraint failure, and not a second role with the same name.

PAST_DATE = (uk_today() - timedelta(days=90)).isoformat()


def _extra_role(app, venue, name="Kitchen"):
    """A second role alongside the "Bar staff" one the venue fixture seeds."""
    with app.app_context():
        conn = db_module.get_db()
        role_id = conn.execute(
            "INSERT INTO venue_role (venue_id, name) VALUES (?, ?)", (venue["id"], name)
        ).lastrowid
        conn.commit()
    return role_id


def _shift_on(app, venue, role_id, on_date, person_id=None, status="scheduled"):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            """INSERT INTO shift (venue_id, person_id, venue_role_id, shift_date, start_time, end_time, status)
               VALUES (?, ?, ?, ?, '18:00', '23:00', ?)""",
            (venue["id"], person_id, role_id, on_date, status),
        )
        conn.commit()


def _role_row(app, role_id):
    with app.app_context():
        conn = db_module.get_db()
        return conn.execute("SELECT * FROM venue_role WHERE id = ?", (role_id,)).fetchone()


def test_delete_role_used_by_a_shift_is_blocked_not_a_crash(app, client, venue):
    """The bug the rest of this section exists for: with foreign_keys=ON the
    DELETE raised an unhandled sqlite3.IntegrityError, reproduced live."""
    kitchen = _extra_role(app, venue)
    _shift_on(app, venue, kitchen, PAST_DATE)
    login_as_pub(client, venue["pub_id"])

    resp = client.post(f"/v/{venue['slug']}/admin/roles/{kitchen}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert b"1 shift(s) still use it" in resp.data
    assert b"Archive it instead" in resp.data
    assert _role_row(app, kitchen) is not None  # still there, not deleted


def test_the_roles_screen_offers_archive_only_where_delete_would_fail(app, client, venue):
    """A Delete button that refuses for nearly every role on the page is
    worse than no button, so each row shows the one that will work."""
    used = _extra_role(app, venue, "Kitchen")
    _shift_on(app, venue, used, PAST_DATE)
    untouched = _extra_role(app, venue, "Cellar")
    login_as_pub(client, venue["pub_id"])

    resp = client.get(f"/v/{venue['slug']}/admin/roles")
    assert f"/roles/{used}/archive".encode() in resp.data
    assert f"/roles/{used}/delete".encode() not in resp.data
    assert f"/roles/{untouched}/delete".encode() in resp.data
    assert f"/roles/{untouched}/archive".encode() not in resp.data


def test_archiving_takes_the_role_off_every_form_that_picks_one(app, client, venue):
    kitchen = _extra_role(app, venue)
    _shift_on(app, venue, kitchen, PAST_DATE)
    login_as_pub(client, venue["pub_id"])

    resp = client.post(f"/v/{venue['slug']}/admin/roles/{kitchen}/archive", follow_redirects=True)
    assert resp.status_code == 200
    assert _role_row(app, kitchen)["archived_at"] is not None

    for url in (
        f"/v/{venue['slug']}/rota/",                             # add an open shift
        f"/v/{venue['slug']}/rota/open-shift/day/{uk_today()}",  # ditto, for one day
        f"/v/{venue['slug']}/admin/staff",                       # invite someone new
        f"/v/{venue['slug']}/rota/leave",                        # scope a blocked date
    ):
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert b">Kitchen<" not in resp.data, url


def test_an_archived_role_still_names_the_shift_it_was_on(app, client, venue):
    """The whole reason for archiving rather than nulling the shifts out:
    last month's rota doesn't forget what the shift was for."""
    kitchen = _extra_role(app, venue)
    # An open shift, because that is where the grid actually prints the
    # role — and where it matters most: the role is what narrows "notify
    # staff" to the people who can work it.
    _shift_on(app, venue, kitchen, PAST_DATE, status="open")
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/admin/roles/{kitchen}/archive")

    resp = client.get(f"/v/{venue['slug']}/rota/?week={PAST_DATE}")
    assert resp.status_code == 200
    assert b"(Kitchen)" in resp.data
    resp = client.get(f"/v/{venue['slug']}/rota/open-shift/day/{PAST_DATE}")
    assert b"Kitchen" in resp.data


def test_a_shift_keeps_a_role_that_has_since_been_archived(app, client, venue):
    """Editing a shift's times must not quietly drop its role: leave the
    archived role out of the <select> and the browser posts nothing for it,
    so the next save clears it."""
    kitchen = _extra_role(app, venue)
    person_id, _membership_id, _pw = create_active_staff(app, venue["id"], name="Sam Cook")
    _shift_on(app, venue, kitchen, PAST_DATE, person_id=person_id)
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/admin/roles/{kitchen}/archive")

    resp = client.get(f"/v/{venue['slug']}/rota/cell/{person_id}/{PAST_DATE}")
    assert resp.status_code == 200
    # Offered on the shift that already has it — and nowhere else on the
    # page, because the "add a shift" form below must not start a new shift
    # off in a role the venue has retired.
    assert resp.data.count(b"Kitchen (archived)") == 1
    assert b">Kitchen<" not in resp.data


def test_a_staff_member_keeps_a_role_that_has_since_been_archived(app, client, venue):
    kitchen = _extra_role(app, venue)
    _person_id, membership_id, _pw = create_active_staff(
        app, venue["id"], name="Sam Cook", role_id=kitchen
    )
    login_as_pub(client, venue["pub_id"])

    resp = client.post(f"/v/{venue['slug']}/admin/roles/{kitchen}/archive", follow_redirects=True)
    # Archiving a role people are still on is allowed — that IS what winding
    # a role down looks like — but it says so rather than leaving them lost.
    assert b"1 staff member(s) are still set to it" in resp.data

    resp = client.get(f"/v/{venue['slug']}/admin/staff/{membership_id}/edit")
    assert resp.status_code == 200
    assert f'value="{kitchen}" selected>Kitchen (archived)'.encode() in resp.data


def test_restoring_a_role_puts_it_back_on_the_forms(app, client, venue):
    kitchen = _extra_role(app, venue)
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/admin/roles/{kitchen}/archive")

    resp = client.post(f"/v/{venue['slug']}/admin/roles/{kitchen}/restore", follow_redirects=True)
    assert resp.status_code == 200
    assert _role_row(app, kitchen)["archived_at"] is None
    assert b">Kitchen<" in client.get(f"/v/{venue['slug']}/admin/staff").data


def test_adding_a_role_by_an_archived_name_restores_the_original(app, client, venue):
    """An archived role isn't on the screen, so re-adding it by name is the
    obvious thing to try. UNIQUE(venue_id, name) would make that a 500 —
    and they mean the old Kitchen, with its shifts still attached."""
    kitchen = _extra_role(app, venue)
    _shift_on(app, venue, kitchen, PAST_DATE)
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/admin/roles/{kitchen}/archive")

    resp = client.post(
        f"/v/{venue['slug']}/admin/roles/create", data={"name": "kitchen"}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert _role_row(app, kitchen)["archived_at"] is None
    with app.app_context():
        conn = db_module.get_db()
        names = [row["name"] for row in conn.execute(
            "SELECT name FROM venue_role WHERE venue_id = ? ORDER BY name", (venue["id"],)
        ).fetchall()]
    assert names == ["Bar staff", "Kitchen"]  # restored, not duplicated


def test_renaming_onto_a_name_already_in_use_is_refused_not_a_crash(app, client, venue):
    """The same UNIQUE constraint from the other direction, and here the
    role in the way can be archived, and so invisible on the screen."""
    kitchen = _extra_role(app, venue)
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/admin/roles/{kitchen}/archive")

    resp = client.post(
        f"/v/{venue['slug']}/admin/roles/{venue['role_id']}/rename",
        data={"name": "Kitchen"}, follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"already a role called" in resp.data
    assert _role_row(app, venue["role_id"])["name"] == "Bar staff"


def test_archiving_is_app_admin_only_like_the_rest_of_role_management(app, client, venue):
    """Retiring a role shapes every rota after it, so it sits with the other
    app_admin-only role routes rather than with day-to-day rota admin."""
    kitchen = _extra_role(app, venue)
    person_id, _membership_id, _email = create_active_staff(
        app, venue["id"], name="Rota Admin Only", permission_level="rota_admin"
    )
    from tests.conftest import login_as_person

    login_as_person(client, person_id)
    for action in ("archive", "restore"):
        resp = client.post(f"/v/{venue['slug']}/admin/roles/{kitchen}/{action}")
        assert resp.status_code == 302, action
    assert _role_row(app, kitchen)["archived_at"] is None
