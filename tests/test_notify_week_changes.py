"""
Customer report, 2026-10-13: once a week's rota had been sent, a later change
to it reached nobody and there was no way to send it again. Now the week reads
"Changed since notified" and only the people whose shifts differ from what
they were sent are messaged.
"""

from datetime import timedelta

import pytest

from app import db as db_module
from app.uk_time import uk_today
from tests.conftest import create_active_staff, login_as_pub

# A week that is still to come: a week that is over is never offered a re-send.
WEEK = uk_today() - timedelta(days=uk_today().weekday()) + timedelta(days=7)
MON, TUE = WEEK.isoformat(), (WEEK + timedelta(days=1)).isoformat()


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr("app.rota_grid.send_email", lambda to, subject, body: sent.append((to, subject, body)))
    monkeypatch.setattr("app.rota_grid.send_sms", lambda to, body: sent.append((to, None, body)))
    return sent


def _shift(app, venue, person_id, shift_date, start="09:00", origin="planned"):
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status, origin) "
            "VALUES (?, ?, ?, ?, '17:00', 'scheduled', ?)",
            (venue["id"], person_id, shift_date, start, origin),
        ).lastrowid
        conn.commit()
    return shift_id


def _sql(app, statement, *params):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(statement, params)
        conn.commit()


def _notified_week(app, client, venue, outbox):
    """Ann and Bob each have Monday 09:00-17:00, and the week has been sent."""
    login_as_pub(client, venue["pub_id"])
    ann, _m, _e = create_active_staff(app, venue["id"], name="Ann")
    bob, _m, _e = create_active_staff(app, venue["id"], name="Bob")
    shifts = {ann: _shift(app, venue, ann, MON), bob: _shift(app, venue, bob, MON)}
    client.post(f"/v/{venue['slug']}/rota/notify-week", data={"week": MON})
    outbox.clear()
    return ann, bob, shifts


def _page(client, venue, week=MON):
    return client.get(f"/v/{venue['slug']}/rota/?week={week}").data.decode("utf-8")


def _resend(client, venue, week=MON):
    return client.post(f"/v/{venue['slug']}/rota/notify-week-changes", data={"week": week},
                       follow_redirects=True).data.decode("utf-8")


def test_an_unchanged_week_stays_notified_with_nothing_to_send(app, client, venue, outbox):
    _notified_week(app, client, venue, outbox)

    html = _page(client, venue)
    assert ">Notified</span>" in html
    assert "Changed since notified" not in html
    assert "notify-week-changes" not in html

    assert "Nothing has changed" in _resend(client, venue)
    assert outbox == []


def test_a_changed_shift_is_flagged_and_only_that_person_is_told(app, client, venue, outbox):
    ann, _bob, shifts = _notified_week(app, client, venue, outbox)
    _sql(app, "UPDATE shift SET start_time = '12:00' WHERE id = ?", shifts[ann])

    html = _page(client, venue)
    assert "Changed since notified" in html
    assert "Notify 1 staff of changes" in html
    assert f'action="/v/{venue["slug"]}/rota/notify-week-changes"' in html

    assert "Notified 1 staff member(s) whose shifts changed" in _resend(client, venue)
    assert [to for to, _s, _b in outbox] == ["ann@example.com"]
    _to, subject, body = outbox[0]
    assert subject.endswith("have changed")
    assert "12:00-17:00" in body and "09:00" not in body

    # Sent, so the week is back to Notified and a second press sends nothing.
    assert "Changed since notified" not in _page(client, venue)
    outbox.clear()
    _resend(client, venue)
    assert outbox == []


def test_someone_added_after_the_week_was_sent_is_told(app, client, venue, outbox):
    _notified_week(app, client, venue, outbox)
    cat, _m, _e = create_active_staff(app, venue["id"], name="Cat")
    _shift(app, venue, cat, TUE)

    _resend(client, venue)
    assert [to for to, _s, _b in outbox] == ["cat@example.com"]


def test_someone_whose_shifts_were_all_removed_is_told(app, client, venue, outbox):
    _ann, bob, shifts = _notified_week(app, client, venue, outbox)
    _sql(app, "DELETE FROM shift WHERE id = ?", shifts[bob])

    assert "Notify 1 staff of changes" in _page(client, venue)
    _resend(client, venue)
    assert [to for to, _s, _b in outbox] == ["bob@example.com"]
    assert "no longer on the rota" in outbox[0][2]
    assert "Changed since notified" not in _page(client, venue)


def test_a_leaver_whose_shifts_were_removed_is_not_messaged(app, client, venue, outbox):
    _ann, bob, shifts = _notified_week(app, client, venue, outbox)
    _sql(app, "DELETE FROM shift WHERE id = ?", shifts[bob])
    _sql(app, "UPDATE venue_membership SET status = 'left' WHERE person_id = ?", bob)

    _resend(client, venue)
    assert outbox == []
    assert "Changed since notified" not in _page(client, venue)


def test_an_unplanned_clock_in_does_not_make_the_week_changed(app, client, venue, outbox):
    ann, _bob, _shifts = _notified_week(app, client, venue, outbox)
    _shift(app, venue, ann, TUE, origin="ad_hoc")

    assert "Changed since notified" not in _page(client, venue)


def test_a_week_that_is_over_is_never_offered_a_resend(app, client, venue, outbox):
    login_as_pub(client, venue["pub_id"])
    ann, _m, _e = create_active_staff(app, venue["id"], name="Ann")
    shift_id = _shift(app, venue, ann, "2026-08-03")
    client.post(f"/v/{venue['slug']}/rota/notify-week", data={"week": "2026-08-03"})
    outbox.clear()
    _sql(app, "UPDATE shift SET start_time = '12:00' WHERE id = ?", shift_id)

    html = _page(client, venue, "2026-08-03")
    assert ">Notified</span>" in html and "notify-week-changes" not in html
    _resend(client, venue, "2026-08-03")
    assert outbox == []


def test_a_week_sent_before_changes_were_tracked_can_be_sent_to_everyone_again(app, client, venue, outbox):
    _notified_week(app, client, venue, outbox)
    _sql(app, "DELETE FROM week_notification_sent")

    html = _page(client, venue)
    assert ">Notified</span>" in html and "Changed since notified" not in html
    assert "rota again</button>" in html

    assert "their shifts for the week" in _resend(client, venue)
    assert sorted(to for to, _s, _b in outbox) == ["ann@example.com", "bob@example.com"]
    assert not outbox[0][1].endswith("have changed")
    # Tracked from here on.
    assert "notify-week-changes" not in _page(client, venue)


def test_an_unnotified_week_cannot_be_sent_as_changes(app, client, venue, outbox):
    login_as_pub(client, venue["pub_id"])
    ann, _m, _e = create_active_staff(app, venue["id"], name="Ann")
    _shift(app, venue, ann, MON)

    assert "been sent to staff yet" in _resend(client, venue)
    assert outbox == []


def test_notify_week_changes_requires_admin_permission(client, venue):
    resp = client.post(f"/v/{venue['slug']}/rota/notify-week-changes", data={"week": MON})
    assert resp.status_code == 302
