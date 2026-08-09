"""
The app fixture disables the rate limiter for every other test (see
conftest.py) so its process-global in-memory counters don't bleed between
unrelated tests. This file is the one place that re-enables it, to prove
the limiter actually trips rather than just trusting the decorator is
there — the exact regression class that bit PricePulse's suite when this
wasn't checked (limiter silently never engaging isn't caught by absence).
"""

from app.extensions import limiter


def test_login_rate_limit_trips_after_too_many_attempts(app, client, venue):
    limiter.enabled = True
    try:
        resp = None
        for _ in range(11):  # login is limited to 10/minute
            resp = client.post(
                f"/v/{venue['slug']}/login",
                data={"identifier": "nobody@example.com", "password": "wrong"},
            )
        assert resp.status_code == 429
    finally:
        limiter.enabled = False
