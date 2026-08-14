from tests.conftest import create_active_staff, login_as_person, login_as_pub


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
