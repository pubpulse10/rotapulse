"""
The `next` a logged-out visitor is bounced to PricePulse's login with.

It used to be interpolated raw into the query string, so any & in the page's
own URL read as the start of the next query parameter and truncated the
target — a landlord bounced off a filtered rota week would come back to the
wrong week, or to nothing. Percent-encoding it is what makes the round trip
exact.

The other half of this — PricePulse actually honouring an absolute URL rather
than dropping it — lives in that repo's tests/test_login_next.py.
"""

from urllib.parse import parse_qs, urlparse

from app.venues import _login_redirect


def test_the_next_target_survives_its_own_query_string(app):
    here = "/v/the-red-lion/rota/week?week=2026-09-07&staff=3"
    with app.test_request_context(here, base_url="https://rotapulse.pubpulse.co.uk"):
        location = _login_redirect().headers["Location"]

    returned = parse_qs(urlparse(location).query)["next"][0]
    assert returned == "https://rotapulse.pubpulse.co.uk" + here
    assert "week=2026-09-07" in returned and "staff=3" in returned


def test_the_next_value_is_encoded_not_raw(app):
    """Belt and braces on the above: the raw & must not appear in the built
    URL outside of the one separating our own parameters."""
    with app.test_request_context(
        "/v/x/rota/week?a=1&b=2", base_url="https://rotapulse.pubpulse.co.uk"
    ):
        location = _login_redirect().headers["Location"]

    assert "%3A%2F%2F" in location          # the :// of the encoded target
    assert location.count("?") == 1         # ...and no second query string
