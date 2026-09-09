"""
The "add RotaPulse to your home screen" nudge.

Real report, 2026-09-09: staff at a venue were opening their original invite
email at the start of every shift, because none of them had managed to get an
icon onto their phone. The app has always been installable — what was missing
was anyone telling them so. These tests pin down where the nudge appears, and
that it never renders already-visible: which of the three sets of steps is
right depends on the browser, and install-hint.js decides that client-side.
"""

from tests.conftest import create_active_staff, login_as_person, login_as_pub


def test_staff_home_offers_the_nudge(app, client, venue):
    person_id, _membership_id, _email = create_active_staff(app, venue["id"])
    login_as_person(client, person_id)

    resp = client.get(f"/v/{venue['slug']}/staff/")

    assert resp.status_code == 200
    assert b'id="install-hint"' in resp.data


def test_login_page_offers_the_nudge(client, venue):
    """The page they land on from the invite — and on iOS the better of the
    two places to install from, because a home-screen app gets its own cookie
    store, so logging in inside it is what leaves them logged in there."""
    resp = client.get(f"/v/{venue['slug']}/login")

    assert resp.status_code == 200
    assert b'id="install-hint"' in resp.data


def test_the_nudge_starts_hidden(client, venue):
    """Rendered hidden and only revealed once the script knows which steps
    apply. A desktop browser that can't install anything must never be shown
    instructions for a phone it isn't."""
    resp = client.get(f"/v/{venue['slug']}/login")

    html = resp.data.decode()
    assert '<div class="install-hint" id="install-hint" hidden>' in html
    for variant in ("install-hint-prompt", "install-hint-ios", "install-hint-android"):
        assert f'id="{variant}" hidden' in html


def test_admin_pages_do_not_carry_the_nudge(client, venue):
    """It's staff-facing copy about a phone. The rota grid is the tablet
    behind the bar, and the script ships with it — so neither should load."""
    login_as_pub(client, venue["pub_id"])

    resp = client.get(f"/v/{venue['slug']}/rota/")

    assert resp.status_code == 200
    assert b'id="install-hint"' not in resp.data
    assert b"install-hint.js" not in resp.data


def test_the_nudge_script_is_served(client):
    resp = client.get("/static/install-hint.js")

    assert resp.status_code == 200
    assert b"beforeinstallprompt" in resp.data
