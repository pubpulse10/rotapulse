"""
Leave types, half days, frozen counts, and telling staff the answer.

Step 1 of docs/leave-design.md, agreed with the owner 15-16 September 2026.
Leave used to be a single undifferentiated "away": no type, no half days, no
balance, and nothing told the person who asked for it what had been decided.

The counting rules are the part worth guarding. A "day" is relative to the
person — a date only counts when they would normally work that weekday — and
what a booking is worth is frozen when it is approved, so that editing an
availability later cannot rewrite last year's holiday.
"""

from datetime import date, timedelta

import pytest

from app import db as db_module
from app.leave import count_days, days_taken_count, working_pattern
from app.uk_time import uk_today
from tests.conftest import create_active_staff, login_as_person, login_as_pub

MON_TO_FRI = '{"mon":true,"tue":true,"wed":true,"thu":true,"fri":true,"sat":false,"sun":false}'
# Mon 3rd to Fri 7th August 2026.
WEEK_START, WEEK_END = "2026-08-03", "2026-08-07"

# A week that has NOT happened yet, for the two cancellation tests. Cancelling
# leave that is already over is now silent (app/leave.py::notify_decision) --
# "you are back on the rota for those dates" about a past week helps nobody --
# so a fixed date in the past would test the suppression, not the message.
_NEXT_MONDAY = uk_today() + timedelta(days=(7 - uk_today().weekday()) or 7)
FUTURE_WEEK_START = _NEXT_MONDAY.isoformat()
FUTURE_WEEK_END = (_NEXT_MONDAY + timedelta(days=4)).isoformat()


@pytest.fixture
def sent(monkeypatch):
    """Everything app.leave would have emailed or texted."""
    import app.leave as leave_module

    outbox = {"email": [], "sms": []}
    monkeypatch.setattr(leave_module, "send_email",
                        lambda to, subject, body: outbox["email"].append((to, subject, body)) or True)
    monkeypatch.setattr(leave_module, "send_sms",
                        lambda to, body: outbox["sms"].append((to, body)) or True)
    return outbox


def _staff(app, venue, name="Leave Tester", availability=MON_TO_FRI):
    person_id, membership_id, _e = create_active_staff(app, venue["id"], name=name)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE rota_staff_detail SET availability = ? WHERE venue_membership_id = ?",
                     (availability, membership_id))
        conn.commit()
    return person_id, membership_id


def _leave_rows(app, person_id):
    with app.app_context():
        return db_module.get_db().execute(
            "SELECT * FROM leave_request WHERE person_id = ? ORDER BY id", (person_id,)
        ).fetchall()


def _request_leave(client, venue, **data):
    payload = {"start_date": WEEK_START, "end_date": WEEK_END}
    payload.update(data)
    return client.post(f"/v/{venue['slug']}/staff/leave", data=payload, follow_redirects=True)


# --------------------------------------------------------------------------- #
# The five types
# --------------------------------------------------------------------------- #

def test_a_request_with_no_type_is_paid_leave_taken_as_full_days(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_person(client, person_id)

    _request_leave(client, venue)

    row = _leave_rows(app, person_id)[0]
    assert row["leave_type"] == "paid"
    assert row["start_portion"] == "full" and row["end_portion"] == "full"


def test_leave_recorded_before_types_existed_reads_as_paid_leave(app, venue):
    """The column default is what migrates the old rows: every one of them was
    paid leave, because it was the only kind the app could record."""
    person_id, _m = _staff(app, venue)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO leave_request (person_id, venue_id, start_date, end_date, status) "
            "VALUES (?, ?, ?, ?, 'approved')",
            (person_id, venue["id"], WEEK_START, WEEK_END),
        )
        conn.commit()

    assert _leave_rows(app, person_id)[0]["leave_type"] == "paid"


def test_staff_can_request_unpaid_leave(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_person(client, person_id)

    _request_leave(client, venue, leave_type="unpaid")

    assert _leave_rows(app, person_id)[0]["leave_type"] == "unpaid"


@pytest.mark.parametrize("leave_type", ["sick", "maternity", "lieu"])
def test_staff_cannot_request_the_admin_only_types(app, client, venue, leave_type):
    """Nobody requests being ill in advance. A <select> that omits them is not
    a guard — this is a form post."""
    person_id, _m = _staff(app, venue)
    login_as_person(client, person_id)

    resp = _request_leave(client, venue, leave_type=leave_type)

    assert b"recorded by your manager" in resp.data
    assert _leave_rows(app, person_id) == []


@pytest.mark.parametrize("leave_type", ["paid", "unpaid", "sick", "maternity", "lieu"])
def test_an_admin_can_record_every_type(app, client, venue, leave_type):
    person_id, _m = _staff(app, venue, name=f"Admin {leave_type}")
    login_as_pub(client, venue["pub_id"])

    client.post(f"/v/{venue['slug']}/rota/leave/create", data={
        "person_id": person_id, "start_date": WEEK_START, "end_date": WEEK_END,
        "leave_type": leave_type, "note": "Recorded by the landlord",
    }, follow_redirects=True)

    row = _leave_rows(app, person_id)[0]
    assert row["leave_type"] == leave_type
    assert row["status"] == "approved"
    assert row["note"] == "Recorded by the landlord"


def test_an_unknown_type_is_refused_rather_than_stored(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])

    resp = client.post(f"/v/{venue['slug']}/rota/leave/create", data={
        "person_id": person_id, "start_date": WEEK_START, "end_date": WEEK_END,
        "leave_type": "sabbatical",
    }, follow_redirects=True)

    assert b"Choose a type of leave" in resp.data
    assert _leave_rows(app, person_id) == []


# --------------------------------------------------------------------------- #
# Half days
# --------------------------------------------------------------------------- #

def test_a_full_working_week_is_five_days_not_seven():
    # Mon 3rd to Sun 9th August for a Monday-to-Friday worker.
    assert count_days(MON_TO_FRI, "2026-08-03", "2026-08-09") == 5.0


def test_half_days_at_each_end_take_half_a_day_off_each():
    assert count_days(MON_TO_FRI, WEEK_START, WEEK_END, "half", "half") == 4.0
    assert count_days(MON_TO_FRI, WEEK_START, WEEK_END, "half", "full") == 4.5


def test_a_single_day_can_only_lose_half_a_day_once():
    """Both ends of a one-day booking are the same day, so deducting at each
    end would turn an afternoon off into no leave at all."""
    assert count_days(MON_TO_FRI, WEEK_START, WEEK_START, "half", "half") == 0.5
    assert count_days(MON_TO_FRI, WEEK_START, WEEK_START, "half", "full") == 0.5
    assert count_days(MON_TO_FRI, WEEK_START, WEEK_START, "full", "half") == 0.5


def test_a_half_day_on_a_day_they_never_work_changes_nothing():
    # Saturday 8th to Sunday 9th, for a Monday-to-Friday worker.
    assert count_days(MON_TO_FRI, "2026-08-08", "2026-08-09", "half", "half") == 0.0


def test_staff_can_book_a_half_day(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_person(client, person_id)

    _request_leave(client, venue, start_date=WEEK_START, end_date=WEEK_START, start_portion="half")

    assert _leave_rows(app, person_id)[0]["start_portion"] == "half"


# --------------------------------------------------------------------------- #
# The availability bug
# --------------------------------------------------------------------------- #

def test_availability_that_was_never_set_is_unknown_not_seven_days_a_week():
    """The bug this replaces: `availability.get(weekday, True)` read a missing
    day as "works it", so somebody with no availability had a week's holiday
    counted as 7 days. Fine while the figure was informational; an argument
    with a staff member once it is a balance."""
    assert working_pattern(None) is None
    assert working_pattern("") is None
    assert working_pattern("{}") is None
    assert count_days(None, WEEK_START, WEEK_END) is None


def test_an_all_false_availability_is_unknown_too():
    """Not a real working week, and counting every holiday as nought days is
    the same silent wrongness pointing the other way."""
    assert working_pattern('{"mon":false,"tue":false,"wed":false,"thu":false,"fri":false,"sat":false,"sun":false}') is None


def test_the_staff_page_says_so_rather_than_showing_a_number(app, client, venue):
    person_id, _m = _staff(app, venue, availability=None)
    login_as_person(client, person_id)

    resp = client.get(f"/v/{venue['slug']}/staff/leave")

    assert resp.status_code == 200
    assert b"can&#39;t be counted until your availability is set" in resp.data \
        or b"can't be counted until your availability is set" in resp.data


def test_days_taken_is_none_when_availability_is_unknown(app, venue):
    person_id, _m = _staff(app, venue, availability=None)
    with app.app_context():
        assert days_taken_count(db_module.get_db(), person_id, None, "01-01") is None


# --------------------------------------------------------------------------- #
# Frozen counts
# --------------------------------------------------------------------------- #

def test_approving_freezes_what_the_booking_is_worth(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_person(client, person_id)
    _request_leave(client, venue)
    leave_id = _leave_rows(app, person_id)[0]["id"]

    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/rota/leave/{leave_id}/approve")

    row = _leave_rows(app, person_id)[0]
    assert row["days_counted"] == 5.0


def test_changing_availability_afterwards_does_not_rewrite_history(app, client, venue):
    """The reason the figures are frozen at approval rather than recomputed on
    every read: somebody moving to three days a week must not retrospectively
    change the holiday they took in the summer."""
    person_id, membership_id = _staff(app, venue)
    login_as_person(client, person_id)
    _request_leave(client, venue)
    leave_id = _leave_rows(app, person_id)[0]["id"]
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/rota/leave/{leave_id}/approve")

    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_staff_detail SET availability = ? WHERE venue_membership_id = ?",
            ('{"mon":true,"tue":true,"wed":true,"thu":false,"fri":false,"sat":false,"sun":false}', membership_id),
        )
        conn.commit()

    assert _leave_rows(app, person_id)[0]["days_counted"] == 5.0


def test_hours_come_from_recently_clocked_shifts_when_no_figure_is_set(app, client, venue):
    """Hours are stored on every leave record from the first version, so the
    hours-based model can be added later without migrating history. Until the
    screen for editing usual daily hours exists, they come from what the
    person has actually been working."""
    person_id, _m = _staff(app, venue)
    recent = (date.today() - timedelta(days=7)).isoformat()
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) "
            "VALUES (?, ?, ?, '12:00', '16:00', 'scheduled')",
            (venue["id"], person_id, recent),
        ).lastrowid
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at) VALUES (?, ?, ?)",
            (shift_id, f"{recent} 12:00:00", f"{recent} 16:00:00"),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/rota/leave/create", data={
        "person_id": person_id, "start_date": WEEK_START, "end_date": WEEK_START,
    }, follow_redirects=True)

    row = _leave_rows(app, person_id)[0]
    assert row["days_counted"] == 1.0
    assert row["hours_counted"] == 4.0  # their usual day, from a 4-hour shift


def test_hours_are_left_blank_when_there_is_nothing_to_go_on(app, client, venue):
    """Better an honest blank than a number somebody might pay against."""
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])

    client.post(f"/v/{venue['slug']}/rota/leave/create", data={
        "person_id": person_id, "start_date": WEEK_START, "end_date": WEEK_START,
    }, follow_redirects=True)

    row = _leave_rows(app, person_id)[0]
    assert row["days_counted"] == 1.0
    assert row["hours_counted"] is None


# --------------------------------------------------------------------------- #
# Only paid leave comes off the holiday count
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("leave_type,expected", [("paid", 5.0), ("unpaid", 0.0), ("sick", 0.0),
                                                 ("maternity", 0.0), ("lieu", 0.0)])
def test_only_paid_leave_reduces_the_holiday_count(app, venue, leave_type, expected):
    person_id, _m = _staff(app, venue, name=f"Counted {leave_type}")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            """INSERT INTO leave_request (person_id, venue_id, start_date, end_date, leave_type, status)
               VALUES (?, ?, ?, ?, ?, 'approved')""",
            (person_id, venue["id"], WEEK_START, WEEK_END, leave_type),
        )
        conn.commit()
        taken = days_taken_count(db_module.get_db(), person_id, MON_TO_FRI, "01-01", today=date(2026, 8, 31))

    assert taken == expected


# --------------------------------------------------------------------------- #
# Telling the staff member
# --------------------------------------------------------------------------- #

def test_approving_leave_tells_the_person_who_asked(app, client, venue, sent):
    person_id, _m = _staff(app, venue, name="Told Approved")
    login_as_person(client, person_id)
    _request_leave(client, venue)
    leave_id = _leave_rows(app, person_id)[0]["id"]

    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/rota/leave/{leave_id}/approve")

    assert len(sent["email"]) == 1
    _to, subject, body = sent["email"][0]
    assert "approved" in subject
    assert "Told Approved" in body and "paid leave" in body
    assert "3 August 2026 to 7 August 2026" in body


def test_declining_says_it_was_not_approved(app, client, venue, sent):
    person_id, _m = _staff(app, venue)
    login_as_person(client, person_id)
    _request_leave(client, venue)
    leave_id = _leave_rows(app, person_id)[0]["id"]

    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/rota/leave/{leave_id}/decline")

    assert "has not been approved" in sent["email"][0][2]


def test_cancelling_already_approved_leave_says_cancelled_not_declined(app, client, venue, sent):
    """Being put back on the rota for a week you had been granted is the more
    important of the two messages, and a different one."""
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/rota/leave/create", data={
        "person_id": person_id, "start_date": FUTURE_WEEK_START, "end_date": FUTURE_WEEK_END,
    }, follow_redirects=True)
    leave_id = _leave_rows(app, person_id)[0]["id"]

    client.post(f"/v/{venue['slug']}/rota/leave/{leave_id}/decline")

    assert "has been cancelled" in sent["email"][0][2]
    assert "back on the rota" in sent["email"][0][2]


def test_the_message_stays_inside_gsm_7(app, client, venue, sent):
    """The same words go out by SMS. One character outside GSM-7 doubles the
    segment count of the whole text (commit 6edf40d)."""
    person_id, _m = _staff(app, venue)
    login_as_person(client, person_id)
    _request_leave(client, venue, start_portion="half", end_portion="half")
    leave_id = _leave_rows(app, person_id)[0]["id"]

    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/rota/leave/{leave_id}/approve")

    body = sent["email"][0][2]
    body.encode("ascii")  # raises if a dash or curly quote crept in
    assert "from midday" in body and "until midday" in body


def test_somebody_invited_by_text_gets_a_text(app, client, venue, sent):
    person_id, membership_id = _staff(app, venue)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE person SET mobile = '07700900123' WHERE id = ?", (person_id,))
        conn.execute("UPDATE app_access SET invite_method = 'sms' WHERE venue_membership_id = ?", (membership_id,))
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/rota/leave/create", data={
        "person_id": person_id, "start_date": FUTURE_WEEK_START, "end_date": FUTURE_WEEK_END,
    }, follow_redirects=True)
    leave_id = _leave_rows(app, person_id)[0]["id"]
    client.post(f"/v/{venue['slug']}/rota/leave/{leave_id}/decline")

    assert sent["sms"] and sent["sms"][0][0] == "07700900123"
    assert sent["email"] == []


# --------------------------------------------------------------------------- #
# Showing the type
# --------------------------------------------------------------------------- #

def test_the_grid_shows_which_kind_of_leave_it_is(app, client, venue):
    """Holiday and sick leave looked identical on the grid, which is no use to
    whoever is staffing the week."""
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/rota/leave/create", data={
        "person_id": person_id, "start_date": WEEK_START, "end_date": WEEK_END, "leave_type": "sick",
    }, follow_redirects=True)

    resp = client.get(f"/v/{venue['slug']}/rota/?week={WEEK_START}")

    assert b"cell-leave leave-sick" in resp.data
    assert b'title="Sick"' in resp.data


def test_the_cell_panel_names_the_type_and_the_note(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/rota/leave/create", data={
        "person_id": person_id, "start_date": WEEK_START, "end_date": WEEK_END,
        "leave_type": "maternity", "note": "Back in the spring",
    }, follow_redirects=True)

    resp = client.get(f"/v/{venue['slug']}/rota/cell/{person_id}/{WEEK_START}")

    assert b"Maternity" in resp.data
    assert b"Back in the spring" in resp.data
