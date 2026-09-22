"""
The Help & feedback widget's forwarding endpoint.

Its whole job is to say who is submitting, so that is what most of this pins:
the browser sends the message, and the pub, email and name are filled in here
from the session. Everything about the message itself — lengths, enums, the
screenshot — is the Hub's business and is tested there.

See pubpulse-hub docs/help-widget-integration.md and its docs/decisions.md D3.
"""

import io

import pytest

from tests.conftest import (create_active_staff, login_as_person, login_as_pub,
                            TEST_PUB_ID)


class FakeHubResponse:
    status_code = 200

    def __init__(self):
        self.seen = {}

    def json(self):
        return {"ok": True, "id": 12, "reference": "#12",
                "ticket_id": 3, "work_item_id": None}


@pytest.fixture
def hub(monkeypatch):
    """Capture what would have been posted to the Hub instead of posting it."""
    import app.help_feedback as module

    sent = {}

    def fake_post(url, data=None, files=None, headers=None, timeout=None):
        sent["url"] = url
        sent["data"] = data
        sent["files"] = files
        sent["headers"] = headers
        return FakeHubResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)
    return sent


def submit(client, venue, **overrides):
    data = {
        "type": "bug",
        "title": "Rota prints with last week's dates",
        "body": "Printed Monday's rota and got the previous week.",
        "route": "/v/testvenue/rota/week",
        "app_version": "abc123",
        "context": '{"device":"computer"}',
    }
    data.update(overrides)
    return client.post(f"/v/{venue['slug']}/help/feedback", data=data,
                       content_type="multipart/form-data")


# --- who is allowed to submit -------------------------------------------

def test_a_visitor_cannot_submit(client, venue, hub):
    resp = submit(client, venue)
    assert resp.status_code == 401
    assert hub == {}


def test_the_owner_can_submit(client, venue, hub):
    login_as_pub(client, venue["pub_id"])
    resp = submit(client, venue)

    assert resp.status_code == 200
    assert resp.get_json()["reference"] == "#12"
    assert hub["url"].endswith("/internal/feedback")


def test_a_staff_member_can_submit(client, app, venue, hub):
    person_id, _membership_id, email = create_active_staff(app, venue["id"])
    login_as_person(client, person_id)

    assert submit(client, venue).status_code == 200
    assert hub["data"]["user_email"] == email


def test_a_support_session_cannot_submit_as_the_customer(client, venue, hub):
    """A PubPulse support session is read-only and has no person row. It must
    not be able to file feedback as the customer whose account it is
    looking at."""
    with client.session_transaction() as sess:
        sess["pricepulse_admin"] = True

    assert submit(client, venue).status_code == 401
    assert hub == {}


# --- what gets sent ------------------------------------------------------

def test_identity_is_added_here_not_taken_from_the_browser(client, venue, hub):
    """The one that matters. A pub_id or email trusted from the form would let
    any signed-in customer file as any other — and the Hub's "Your
    conversations" keys off exactly those values, so it would be a way to read
    a stranger's support replies too."""
    login_as_pub(client, venue["pub_id"])

    submit(client, venue, pub_id="999", user_email="attacker@example.com",
           user_name="Not Them", app="pricepulse")

    assert hub["data"]["pub_id"] == TEST_PUB_ID
    assert hub["data"]["user_email"] == "owner@example.com"
    assert hub["data"]["user_name"] == "Owner Person"
    # `app` is ours to state as well — a report cannot be filed against a
    # different app than the one it came from.
    assert hub["data"]["app"] == "rotapulse"


def test_only_whitelisted_fields_are_forwarded(client, venue, hub):
    login_as_pub(client, venue["pub_id"])
    submit(client, venue, something_new="should not be forwarded")

    assert "something_new" not in hub["data"]
    assert hub["data"]["title"] == "Rota prints with last week's dates"
    assert hub["data"]["context"] == '{"device":"computer"}'


def test_the_shared_secret_authenticates_the_hop(client, venue, hub):
    login_as_pub(client, venue["pub_id"])
    submit(client, venue)

    assert hub["headers"]["Authorization"].startswith("Bearer ")


def test_a_screenshot_is_streamed_through_not_stored(client, venue, hub):
    """Never saved here and never re-encoded — the Hub does all of that
    (pubpulse-hub D11). A copy kept here would be a second store of customer
    screenshots with none of those checks done to it."""
    login_as_pub(client, venue["pub_id"])

    client.post(
        f"/v/{venue['slug']}/help/feedback",
        data={
            "type": "bug", "title": "Broken", "body": "See attached.",
            "screenshot": (io.BytesIO(b"\x89PNG\r\n\x1a\nfake"), "shot.png",
                           "image/png"),
        },
        content_type="multipart/form-data",
    )

    assert "screenshot" in hub["files"]


def test_the_hubs_answer_is_passed_back_untouched(client, venue, monkeypatch):
    """The widget shows errors[0] to the customer. Re-wording it here would
    mean two places deciding what a too-long title is called."""
    import app.help_feedback as module

    class Rejected:
        status_code = 400

        @staticmethod
        def json():
            return {"error": "invalid", "errors": ["title is required"]}

    monkeypatch.setattr(module.requests, "post",
                        lambda *a, **k: Rejected())
    login_as_pub(client, venue["pub_id"])

    resp = submit(client, venue)
    assert resp.status_code == 400
    assert resp.get_json()["errors"] == ["title is required"]


def test_an_unreachable_hub_says_so_rather_than_500ing(client, venue, monkeypatch):
    import requests as requests_module

    import app.help_feedback as module

    def boom(*_a, **_k):
        raise requests_module.RequestException("hub down")

    monkeypatch.setattr(module.requests, "post", boom)
    login_as_pub(client, venue["pub_id"])

    resp = submit(client, venue)
    assert resp.status_code == 502
    assert "try again" in resp.get_json()["error"]


# --- the page side -------------------------------------------------------

def test_the_widget_is_on_the_page_for_a_signed_in_person(client, venue):
    login_as_pub(client, venue["pub_id"])
    page = client.get(f"/v/{venue['slug']}/rota/").get_data(as_text=True)

    assert "help-widget.js" in page
    assert 'data-app="rotapulse"' in page
    # It must point at THIS app, never at the Hub (D3).
    assert f'data-endpoint="/v/{venue["slug"]}/help/feedback"' in page
    assert 'id="pp-help-mount"' in page


def test_the_widget_is_not_offered_to_a_visitor(client, venue):
    page = client.get(f"/v/{venue['slug']}/rota/").get_data(as_text=True)
    assert "help-widget.js" not in page


def test_the_hub_origin_is_in_the_script_csp(client, venue):
    """Report-only today, so a missing entry would break nothing now and
    everything the day the policy is enforced."""
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/rota/")

    csp = resp.headers["Content-Security-Policy-Report-Only"]
    script_src = [p for p in csp.split(";") if p.strip().startswith("script-src")][0]
    assert "pubpulse.co.uk" in script_src


def test_the_endpoint_keeps_its_csrf_protection(client, app, venue, hub):
    """A browser POST on our own origin is an ordinary CSRF target — this must
    not end up on the exempt blueprint with /internal."""
    app.config["WTF_CSRF_ENABLED"] = True
    login_as_pub(client, venue["pub_id"])

    assert submit(client, venue).status_code == 400
    assert hub == {}
