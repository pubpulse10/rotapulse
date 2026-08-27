"""
Coverage for the family-admin support view (app/family_admin.py, the
"support_readonly" permission level in app/rota_auth.py) — read-only
cross-venue access for a session carrying session['pricepulse_admin'],
set by PricePulse's own /admin/login and readable here via the shared
PubPulse session cookie.
"""


def login_as_family_admin(client):
    with client.session_transaction() as sess:
        sess["pricepulse_admin"] = True


def test_family_admin_reaches_any_venues_rota_without_local_login(client, venue):
    login_as_family_admin(client)
    resp = client.get(f"/v/{venue['slug']}/rota/")
    assert resp.status_code == 200


def test_family_admin_write_action_is_blocked(client, venue):
    login_as_family_admin(client)
    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/create",
        data={"person_id": "1", "date": "2026-08-01", "start_time": "09:00", "end_time": "17:00"},
    )
    assert resp.status_code == 403


def test_family_admin_cannot_reach_staff_only_route(client, venue):
    """staff_portal routes accept 'staff' alongside 'rota_admin'/'app_admin'
    and dereference g.person unguarded — the support bypass must exclude
    these rather than crash on g.person being None."""
    login_as_family_admin(client)
    resp = client.get(f"/v/{venue['slug']}/staff/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_family_admin_rota_week_hides_mutating_buttons(client, venue):
    """The week grid's Notify/Copy/Clear buttons live directly on the page
    now (2026-08-19, moved off the old nav dropdown, which used to guard
    these the same way) — the support-readonly bypass in require_permission
    means these would 403 if actually clicked (see
    test_family_admin_write_action_is_blocked above), but showing live
    buttons that just error on click is bad UX for a read-only session."""
    login_as_family_admin(client)
    resp = client.get(f"/v/{venue['slug']}/rota/")
    assert resp.status_code == 200
    assert b"Notify staff of this week" not in resp.data
    assert b"Copy Week" not in resp.data
    assert b"Clear week" not in resp.data
    assert b"Read-only support view" in resp.data


def test_without_the_flag_venues_list_is_forbidden(client):
    resp = client.get("/admin/venues")
    assert resp.status_code == 403


def test_family_admin_sees_the_venues_list(client, venue):
    login_as_family_admin(client)
    resp = client.get("/admin/venues")
    assert resp.status_code == 200
    assert b"Test Venue" in resp.data


def test_ordinary_local_staff_session_cannot_reach_the_venues_list(client, venue):
    from tests.conftest import create_active_staff, login_as_person

    person_id, _, _ = create_active_staff(client.application, venue["id"])
    login_as_person(client, person_id)
    resp = client.get("/admin/venues")
    assert resp.status_code == 403
