"""
A support session can look at everything and save nothing — and now says so.

Reported live, 2026-09-17. The owner logged in at PricePulse's /admin/login
to reach its cross-pub admin. That sets `pricepulse_admin`, which is shared
across the whole family on the .pubpulse.co.uk cookie, so RotaPulse
immediately treated the same browser as a read-only SUPPORT session
(app/rota_auth.py::require_permission).

Every admin screen still opened, looking completely normal. A venue setting
was ticked, Save was pressed — and the save was refused with Werkzeug's
default "Forbidden" page: no explanation, no way forward, and no clue that
the cause was a session rather than a broken app.

Two things were wrong, and both are fixed here:

* The refusal said nothing. It is still a 403 — the request genuinely was
  refused and nothing was written — but it now explains why and offers the
  way out, the same reasoning as csrf_error.html.
* The control was offered at all. A screen that cannot save should not be
  showing a Save button, because somebody who misses the error page believes
  the change went through. For a setting that governs what STAFF are told
  about their holiday, that is worse than an error.
"""

from app import db as db_module
from tests.conftest import create_active_staff, login_as_pub

SETTINGS_FORM = {
    "venue_name": "Test Venue",
    "pay_period_type": "weekly",
    "holiday_year_start_day": "1",
    "holiday_year_start_month": "1",
    "leave_figures_provisional": "on",
}


def login_as_support(client):
    """What PricePulse's /admin/login leaves in the shared session cookie."""
    with client.session_transaction() as sess:
        sess.clear()
        sess["pricepulse_admin"] = True


def _provisional(app, venue):
    with app.app_context():
        return db_module.get_db().execute(
            "SELECT leave_figures_provisional FROM venue_settings WHERE venue_id = ?", (venue["id"],)
        ).fetchone()["leave_figures_provisional"]


# --------------------------------------------------------------------------- #
# The refusal explains itself
# --------------------------------------------------------------------------- #

def test_saving_a_setting_is_refused_with_an_explanation_not_a_bare_forbidden(app, client, venue):
    login_as_support(client)

    resp = client.post(f"/v/{venue['slug']}/admin/settings", data=SETTINGS_FORM)

    assert resp.status_code == 403          # it really was refused
    html = resp.data.decode("utf-8")
    assert "support session" in html
    assert "Nothing was saved" in html


def test_the_refusal_offers_the_way_out(app, client, venue):
    """The session key lives in PricePulse and is shared by cookie, so only
    PricePulse can clear it — a person stuck here cannot fix it from here
    without being told where to go."""
    login_as_support(client)

    html = client.post(f"/v/{venue['slug']}/admin/settings", data=SETTINGS_FORM).data.decode("utf-8")

    assert "/admin/logout" in html
    assert "Sign out of support" in html


def test_nothing_is_actually_written(app, client, venue):
    login_as_support(client)

    client.post(f"/v/{venue['slug']}/admin/settings", data=SETTINGS_FORM)

    assert _provisional(app, venue) == 0


def test_the_same_save_works_for_the_venues_own_admin(app, client, venue):
    """The guard must not be catching ordinary owners."""
    login_as_pub(client, venue["pub_id"])

    resp = client.post(f"/v/{venue['slug']}/admin/settings", data=SETTINGS_FORM,
                       follow_redirects=True)

    assert resp.status_code == 200
    assert _provisional(app, venue) == 1


# --------------------------------------------------------------------------- #
# Looking still works
# --------------------------------------------------------------------------- #

def test_a_support_session_can_still_open_the_admin_screens(app, client, venue):
    """Read-only is the point of the session, not a punishment."""
    login_as_support(client)

    assert client.get(f"/v/{venue['slug']}/admin/settings").status_code == 200
    assert client.get(f"/v/{venue['slug']}/rota/leave").status_code == 200


def test_every_page_says_it_is_read_only(app, client, venue):
    """The screens look completely normal otherwise, which is what caught the
    owner out."""
    login_as_support(client)

    html = client.get(f"/v/{venue['slug']}/admin/settings").data.decode("utf-8")

    assert "read-only" in html.lower()
    assert "Viewing as PubPulse support" in html


def test_the_controls_are_switched_off_rather_than_left_looking_live(app, client, venue):
    login_as_support(client)

    html = client.get(f"/v/{venue['slug']}/admin/settings").data.decode("utf-8")

    assert "readonly-guard.js" in html


def test_an_ordinary_admin_gets_no_banner_and_no_guard(app, client, venue):
    login_as_pub(client, venue["pub_id"])

    html = client.get(f"/v/{venue['slug']}/admin/settings").data.decode("utf-8")

    assert "Viewing as PubPulse support" not in html
    assert "readonly-guard.js" not in html


def test_a_staff_facing_page_is_not_affected(app, client, venue):
    """staff_portal dereferences g.person unguarded and a support session has
    none, so those routes were always excluded from the bypass — they bounce
    to a login rather than rendering."""
    create_active_staff(app, venue["id"], name="Not Support")
    login_as_support(client)

    resp = client.get(f"/v/{venue['slug']}/staff/leave")

    assert resp.status_code == 302
