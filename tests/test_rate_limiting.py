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


def test_attempts_are_counted_against_the_account_not_only_the_address(app, client, venue):
    """The per-address limit rests on a header we cannot prove the edge always
    overwrites (2026-10-07). A dozen attempts on one login lock it for a while,
    wherever they come from; another login is unaffected."""
    def wrong(identifier, n):
        return client.post(
            f"/v/{venue['slug']}/login",
            data={"identifier": identifier, "password": "wrong"},
            headers={"CF-Connecting-IP": f"203.0.113.{n}"},
        ).status_code

    limiter.enabled = True
    try:
        limiter.reset()
        statuses = [wrong("target@example.com", n) for n in range(13)]
        other = wrong("someone-else@example.com", 12)
    finally:
        limiter.reset()
        limiter.enabled = False

    assert statuses[:12] == [200] * 12 and statuses[12] == 429
    assert other == 200
