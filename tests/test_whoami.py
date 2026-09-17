"""
Coverage for the identity chip (app/whoami.py, templates/_whoami.html) — the
nav's answer to "who am I signed in as, and what am I allowed to do here?".

It also carries the log-out fix: the old nav only offered the POST log-out to
people invited through RotaPulse's OWN staff login, so a venue owner — whose
session is the shared PubPulse cookie, which that POST doesn't touch — had no
way to log out of RotaPulse at all.
"""

from tests.conftest import create_active_staff, login_as_person, login_as_pub

WEEK = "/v/{slug}/dashboard/"


def test_owner_sees_the_owner_chip_and_a_working_log_out(client, venue):
    login_as_pub(client, venue["pub_id"])
    body = client.get(WEEK.format(slug=venue["slug"])).get_data(as_text=True)

    assert "pp-id-owner" in body
    assert "Owner Person" in body
    assert "owner@example.com" in body
    # Only the Hub's /logout clears the shared family cookie; the local POST
    # log-out would have left them signed in.
    assert "/logout" in body
    assert "pp-id-out" in body


def test_only_the_owner_is_offered_people_and_access(client, app, venue):
    login_as_pub(client, venue["pub_id"])
    assert "People &amp; access" in client.get(WEEK.format(slug=venue["slug"])).get_data(as_text=True)

    person_id, _, _ = create_active_staff(app, venue["id"], permission_level="rota_admin")
    login_as_person(client, person_id)
    body = client.get(WEEK.format(slug=venue["slug"])).get_data(as_text=True)
    assert "People &amp; access" not in body


def test_rota_admin_reads_as_manager(client, app, venue):
    person_id, _, _ = create_active_staff(
        app, venue["id"], name="Lisa Hart", permission_level="rota_admin"
    )
    login_as_person(client, person_id)
    body = client.get(WEEK.format(slug=venue["slug"])).get_data(as_text=True)

    assert "pp-id-manager" in body
    assert "pp-id-owner" not in body
    assert "Lisa Hart" in body


def test_locally_invited_staff_get_the_post_log_out(client, app, venue):
    """Their session is RotaPulse's own, not the family cookie, so logging
    them out means clearing that — which app/rota_login.py only does on POST."""
    person_id, _, _ = create_active_staff(app, venue["id"], name="Sam Field")
    login_as_person(client, person_id)
    body = client.get(f"/v/{venue['slug']}/staff/").get_data(as_text=True)

    assert "pp-id-staff" in body
    assert "Sam Field" in body
    assert 'method="post"' in body
    assert "/logout" in body


def test_support_session_is_badged_and_can_get_out_of_it(client, venue):
    with client.session_transaction() as sess:
        sess["pricepulse_admin"] = True
    body = client.get(f"/v/{venue['slug']}/admin/staff").get_data(as_text=True)

    assert "pp-id-support" in body
    # Ending a support session means ending it where PricePulse started it.
    assert "/admin/logout" in body


def test_no_session_means_no_chip(client, venue):
    body = client.get(f"/v/{venue['slug']}/", follow_redirects=True).get_data(as_text=True)
    assert "pp-id-" not in body
