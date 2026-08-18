from tests.conftest import TEST_STAFF_PASSWORD, create_active_staff, login_as_person, login_as_pub


def test_root_redirects_owner_to_their_venue(client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.get("/")
    assert resp.status_code == 302
    assert f"/v/{venue['slug']}/rota/" in resp.headers["Location"]


def test_root_redirects_logged_in_staff_to_their_venue_home(app, client, venue):
    """The actual bug report: a staff member's home-screen icon opens '/'
    with no venue slug to work from, and she has no session['pub_id'] at
    all (she logs in locally, not via PricePulse) — the route used to
    assume that combination meant "not logged in yet" and always bounced
    to PricePulse's owner-only login, which is wrong for staff."""
    person_id, _membership_id, _email = create_active_staff(app, venue["id"], name="Lianne Fairweather")
    login_as_person(client, person_id)

    resp = client.get("/")
    assert resp.status_code == 302
    assert f"/v/{venue['slug']}/staff/" in resp.headers["Location"]


def test_root_falls_back_to_pricepulse_login_with_no_session_at_all(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert "pricepulse" in resp.headers["Location"].lower() or "login" in resp.headers["Location"].lower()


def test_local_login_sets_a_remembered_slug_cookie(app, client, venue):
    person_id, _m, email = create_active_staff(app, venue["id"], name="Cookie Test")
    resp = client.post(
        f"/v/{venue['slug']}/login", data={"identifier": email, "password": TEST_STAFF_PASSWORD}
    )
    assert resp.status_code == 302
    assert client.get_cookie("rotapulse_slug").value == venue["slug"]


def test_root_uses_remembered_slug_cookie_once_the_session_is_gone(app, client, venue):
    """Real report, 2026-08-18: a staff member was being sent back to the
    owner-only PricePulse login even after logging in locally before --
    her session had genuinely expired/cleared (not just this specific
    page), and the prior fix only covered a session that was still valid.
    A plain cookie set at login time (not tied to session validity) is
    what's actually needed to survive that."""
    person_id, _m, email = create_active_staff(app, venue["id"], name="Cookie Test 2")
    client.post(f"/v/{venue['slug']}/login", data={"identifier": email, "password": TEST_STAFF_PASSWORD})

    with client.session_transaction() as sess:
        sess.clear()  # simulates the session itself expiring/being cleared

    resp = client.get("/")
    assert resp.status_code == 302
    assert f"/v/{venue['slug']}/login" in resp.headers["Location"]
    assert "pricepulse" not in resp.headers["Location"].lower()
