"""
Entering a leave history, and being able to see and undo it.

Real need, 2026-09-17: The Cock started using RotaPulse partway through its
holiday year, so several staff had taken leave the app knows nothing about —
Lianne among them — and the balances were wrong for everybody affected.

Two gaps this closes, both of which only appear once a pub starts mid-year:

* The leave queue lists only CURRENT AND UPCOMING approved leave, so a past
  booking is invisible there. Including one just typed in by mistake, which
  therefore could not be taken back.
* It takes one booking per submission, and a year of somebody's holiday is
  half a dozen separate periods, per person.
"""

from datetime import timedelta

from app import db as db_module
from app.uk_time import uk_today
from tests.conftest import create_active_staff, login_as_person, login_as_pub

MON_TO_FRI = '{"mon":true,"tue":true,"wed":true,"thu":true,"fri":true,"sat":false,"sun":false}'


def _staff(app, venue, name="Lianne B", availability=MON_TO_FRI):
    person_id, membership_id, _e = create_active_staff(app, venue["id"], name=name)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE rota_staff_detail SET availability = ? WHERE venue_membership_id = ?",
                     (availability, membership_id))
        conn.commit()
    return person_id, membership_id


def _post(client, venue, person_id, rows, **extra):
    """rows: list of (start, end) or (start, end, type)."""
    data = {"person_id": person_id}
    for index, row in enumerate(rows):
        data[f"start_date_{index}"] = row[0]
        data[f"end_date_{index}"] = row[1]
        if len(row) > 2:
            data[f"leave_type_{index}"] = row[2]
    data.update(extra)
    return client.post(f"/v/{venue['slug']}/rota/leave/history", data=data, follow_redirects=True)


def _leave(app, person_id):
    with app.app_context():
        return db_module.get_db().execute(
            "SELECT * FROM leave_request WHERE person_id = ? AND status = 'approved' ORDER BY start_date",
            (person_id,),
        ).fetchall()


# --------------------------------------------------------------------------- #
# Entering several periods at once
# --------------------------------------------------------------------------- #

def test_several_periods_go_in_from_one_submission(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])

    _post(client, venue, person_id, [
        ("2026-02-09", "2026-02-13"),
        ("2026-04-06", "2026-04-10"),
        ("2026-06-01", "2026-06-05"),
    ])

    rows = _leave(app, person_id)
    assert len(rows) == 3
    assert [r["start_date"] for r in rows] == ["2026-02-09", "2026-04-06", "2026-06-01"]
    assert all(r["status"] == "approved" for r in rows)


def test_blank_rows_are_skipped(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])

    _post(client, venue, person_id, [("2026-02-09", "2026-02-13"), ("", "")])

    assert len(_leave(app, person_id)) == 1


def test_what_each_period_counted_as_is_reported_back(app, client, venue):
    """The figure is frozen here and never recalculated, so seeing "5 days"
    against a week is the only chance to notice a wrong working pattern
    before the number sets."""
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])

    html = _post(client, venue, person_id, [("2026-02-09", "2026-02-13")]).data.decode("utf-8")

    assert "9 February 2026 to 13 February 2026 (5 days)" in html


def test_a_period_that_cannot_be_counted_says_so_rather_than_reporting_nought(app, client, venue):
    person_id, _m = _staff(app, venue, availability=None)
    login_as_pub(client, venue["pub_id"])

    html = _post(client, venue, person_id, [("2026-02-09", "2026-02-13")]).data.decode("utf-8")

    assert "working days not set" in html


def test_entering_the_same_week_twice_does_not_double_their_holiday(app, client, venue):
    """The obvious mistake when working down a list on paper."""
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])
    _post(client, venue, person_id, [("2026-02-09", "2026-02-13")])

    html = _post(client, venue, person_id, [("2026-02-09", "2026-02-13")]).data.decode("utf-8")

    assert len(_leave(app, person_id)) == 1
    assert "Already had leave over these dates" in html


def test_a_backwards_row_is_named_and_the_good_rows_still_go_in(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])

    html = _post(client, venue, person_id, [
        ("2026-02-13", "2026-02-09"),
        ("2026-04-06", "2026-04-10"),
    ]).data.decode("utf-8")

    assert len(_leave(app, person_id)) == 1
    assert "row 1" in html


def test_the_type_can_differ_row_by_row(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])

    _post(client, venue, person_id, [
        ("2026-02-09", "2026-02-13", "paid"),
        ("2026-04-06", "2026-04-07", "sick"),
    ])

    assert [r["leave_type"] for r in _leave(app, person_id)] == ["paid", "sick"]


def test_recording_a_history_tells_the_staff_member_nothing(app, client, venue, monkeypatch):
    """Texting somebody about holiday they took in February would be absurd."""
    sent = []
    import app.leave as leave_module
    monkeypatch.setattr(leave_module, "send_email", lambda *a: sent.append(a) or True)
    monkeypatch.setattr(leave_module, "send_sms", lambda *a: sent.append(a) or True)
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])

    _post(client, venue, person_id, [("2026-02-09", "2026-02-13")])

    assert sent == []


def test_it_counts_towards_the_holiday_position(app, client, venue):
    """The whole point: the balances were wrong until this went in."""
    person_id, membership_id = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])

    _post(client, venue, person_id, [("2026-02-09", "2026-02-13")])
    html = client.get(f"/v/{venue['slug']}/rota/leave/history?person_id={person_id}").data.decode("utf-8")

    position = html[html.index("this holiday year"):]
    assert ">5<" in position    # taken
    assert ">23<" in position   # 28 allowance less those 5


# --------------------------------------------------------------------------- #
# Seeing and undoing what is there
# --------------------------------------------------------------------------- #

def test_past_leave_is_listed_where_the_leave_queue_will_not_show_it(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])
    _post(client, venue, person_id, [("2026-02-09", "2026-02-13")])

    history = client.get(f"/v/{venue['slug']}/rota/leave/history?person_id={person_id}")
    queue = client.get(f"/v/{venue['slug']}/rota/leave")

    assert b"9 February 2026" in history.data
    assert b"9 February 2026" not in queue.data  # current and upcoming only, by design


def test_a_mistyped_period_can_be_taken_back(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])
    _post(client, venue, person_id, [("2026-02-09", "2026-02-13")])
    leave_id = _leave(app, person_id)[0]["id"]

    client.post(f"/v/{venue['slug']}/rota/leave/{leave_id}/decline", follow_redirects=True)

    assert _leave(app, person_id) == []


def test_taking_back_finished_leave_does_not_text_them(app, client, venue, monkeypatch):
    """"You are back on the rota for those dates" about a week in February
    helps nobody and alarms whoever gets it."""
    sent = []
    import app.leave as leave_module
    monkeypatch.setattr(leave_module, "send_email", lambda *a: sent.append(a) or True)
    monkeypatch.setattr(leave_module, "send_sms", lambda *a: sent.append(a) or True)
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])
    _post(client, venue, person_id, [("2026-02-09", "2026-02-13")])
    leave_id = _leave(app, person_id)[0]["id"]

    client.post(f"/v/{venue['slug']}/rota/leave/{leave_id}/decline", follow_redirects=True)

    assert sent == []


def test_cancelling_leave_that_has_not_happened_yet_still_tells_them(app, client, venue, monkeypatch):
    """The suppression above must not swallow the message that matters: they
    really are back on the rota, and may not expect to be."""
    sent = []
    import app.leave as leave_module
    monkeypatch.setattr(leave_module, "send_email", lambda *a: sent.append(a) or True)
    monkeypatch.setattr(leave_module, "send_sms", lambda *a: sent.append(a) or True)
    person_id, _m = _staff(app, venue)
    ahead = uk_today() + timedelta(days=30)
    login_as_pub(client, venue["pub_id"])
    _post(client, venue, person_id, [(ahead.isoformat(), ahead.isoformat())])
    leave_id = _leave(app, person_id)[0]["id"]

    client.post(f"/v/{venue['slug']}/rota/leave/{leave_id}/decline", follow_redirects=True)

    assert len(sent) == 1


def test_somebody_who_has_left_can_still_have_their_history_entered(app, client, venue):
    """They took leave in February whether or not they work here in
    September, and the year's figures are wrong without it."""
    person_id, membership_id = _staff(app, venue, name="Gone Away")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE venue_membership SET status = 'left' WHERE id = ?", (membership_id,))
        conn.commit()
    login_as_pub(client, venue["pub_id"])

    _post(client, venue, person_id, [("2026-02-09", "2026-02-13")])

    assert len(_leave(app, person_id)) == 1


def test_staff_cannot_reach_it(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_person(client, person_id)

    resp = client.get(f"/v/{venue['slug']}/rota/leave/history")

    assert resp.status_code == 302


def test_staff_cannot_post_a_history_for_themselves(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_person(client, person_id)

    _post(client, venue, person_id, [("2026-02-09", "2026-02-13")])

    assert _leave(app, person_id) == []


def test_the_leave_queue_links_to_it(app, client, venue):
    login_as_pub(client, venue["pub_id"])

    resp = client.get(f"/v/{venue['slug']}/rota/leave")

    assert f"/v/{venue['slug']}/rota/leave/history".encode() in resp.data
