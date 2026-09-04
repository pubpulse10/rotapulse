"""Real report, 2026-09-04: a staff member could not clock out.

She clocked in at 08:06, the phone sat on the shift page for the whole
shift, and the clock-out at 10:31 returned an unstyled "Bad Request — The
CSRF token has expired". Flask-WTF's default WTF_CSRF_TIME_LIMIT is 3600
seconds, so the token rendered into the page at clock-in was already two and
a half hours stale by the time she pressed the button. Every shift longer
than an hour was affected.

The rest of the suite runs with WTF_CSRF_ENABLED = False (see conftest), so
nothing here was ever exercised — which is exactly why this shipped. These
tests turn CSRF back on and drive the real thing.
"""

import re
import time as real_time
from datetime import date

import itsdangerous.timed
import pytest

from app import db as db_module
from tests.conftest import create_active_staff, login_as_person

TOKEN_RE = re.compile(rb'name="csrf-token" content="([^"]+)"')


class _ShiftedClock:
    """Stands in for the `time` module inside itsdangerous, so a request can be
    made to arrive hours after the page that produced it was rendered.

    Note this has to run the clock FORWARD over the submission rather than
    backward over the render: Flask signs its own session cookie with the same
    library, and itsdangerous rejects a negative signature age outright, so a
    backdated render just loses the session and redirects to the login page.
    Forward is the honest direction anyway — the page is old, not the POST."""

    def __init__(self, offset_seconds):
        self.offset_seconds = offset_seconds

    def time(self):
        return real_time.time() + self.offset_seconds


@pytest.fixture
def csrf_app(app):
    """The standard app fixture with CSRF protection switched back on."""
    app.config["WTF_CSRF_ENABLED"] = True
    return app


def _create_shift_for(app, venue_id, person_id):
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status)"
            " VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue_id, person_id, date.today().isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def _token_on_page(resp):
    match = TOKEN_RE.search(resp.data)
    assert match, "no CSRF token rendered on the page"
    return match.group(1).decode()


def test_clock_out_works_from_a_page_rendered_hours_earlier(csrf_app, client, venue, monkeypatch):
    """The reported bug, end to end: render the shift page as it would have
    been rendered at clock-in, then clock out from it hours later."""
    person_id, _m, _e = create_active_staff(csrf_app, venue["id"])
    shift_id = _create_shift_for(csrf_app, venue["id"], person_id)
    login_as_person(client, person_id)
    shift_url = f"/v/{venue['slug']}/staff/shift/{shift_id}"

    clock_in = client.post(
        f"{shift_url}/clock-in",
        data={"csrf_token": _token_on_page(client.get(shift_url)), "lat": "52.6", "lng": "1.3"},
    )
    assert clock_in.status_code == 302, "clock-in itself failed, so the clock-out below proves nothing"

    # The page the phone is left sitting on for the shift — the one the
    # clock-in redirect lands you on, showing "Clocked in at ...".
    token_from_the_start_of_the_shift = _token_on_page(client.get(shift_url))

    # ...and the shift passes.
    with monkeypatch.context() as patched:
        patched.setattr(itsdangerous.timed, "time", _ShiftedClock(5 * 3600))
        resp = client.post(
            f"{shift_url}/clock-out",
            data={"csrf_token": token_from_the_start_of_the_shift, "lat": "52.6", "lng": "1.3"},
        )
    assert resp.status_code == 302, "a five-hour-old page was rejected — the 2026-09-04 bug is back"

    with csrf_app.app_context():
        att = db_module.get_db().execute(
            "SELECT clock_out_at FROM attendance WHERE shift_id = ?", (shift_id,)
        ).fetchone()
        assert att["clock_out_at"] is not None


def test_csrf_time_limit_survives_csrfprotect_init(app):
    """CSRFProtect.init_app does app.config.setdefault("WTF_CSRF_TIME_LIMIT",
    3600). config.apply() runs first precisely so the explicit None wins — if
    the two are ever reordered, the hour comes back silently."""
    assert app.config["WTF_CSRF_TIME_LIMIT"] is None


def test_a_rejected_token_gets_a_page_someone_can_act_on(csrf_app, client, venue):
    """A CSRF failure can still happen for other reasons (session cleared,
    cookie dropped). Werkzeug's default body is an unstyled white page
    reading "Bad Request", which strands whoever hit it."""
    person_id, _m, _e = create_active_staff(csrf_app, venue["id"])
    shift_id = _create_shift_for(csrf_app, venue["id"], person_id)
    login_as_person(client, person_id)

    resp = client.post(
        f"/v/{venue['slug']}/staff/shift/{shift_id}/clock-out",
        data={"csrf_token": "not-a-real-token"},
    )
    assert resp.status_code == 400
    assert b"Nothing was saved" in resp.data
    assert b"The CSRF token" not in resp.data
