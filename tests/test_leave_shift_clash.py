"""
A shift left on the rota inside somebody's approved leave, and the week's
rostered hours under each name.

Customer report, 2026-11-19: "When a rota has been made, if a member of staff
requests time off, the rota does not show the holiday. You also don't get a
notification to say a member of staff is on holiday but has a shift."

The shift is never removed automatically. What these tests hold is that the
clash is flagged everywhere a manager would look: on the week grid, against
the request before it is approved, in the message after approving or adding
leave, and against approved leave until the rota is changed.
"""

from datetime import timedelta

from app import db as db_module
from app.leave import shift_clashes_with_leave
from app.uk_time import uk_today
from tests.conftest import create_active_staff, login_as_person, login_as_pub


def _next_monday():
    today = uk_today()
    return today + timedelta(days=7 - today.weekday())


def _shift(app, venue, person_id, on, start="10:30", end="17:00"):
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) "
            "VALUES (?, ?, ?, ?, ?, 'scheduled')",
            (venue["id"], person_id, on.isoformat(), start, end),
        ).lastrowid
        conn.commit()
    return shift_id


def _leave(app, venue, person_id, start, end, status="approved", leave_type="paid",
           start_portion="full", end_portion="full"):
    with app.app_context():
        conn = db_module.get_db()
        leave_id = conn.execute(
            "INSERT INTO leave_request (person_id, venue_id, start_date, end_date, leave_type, "
            "start_portion, end_portion, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (person_id, venue["id"], start.isoformat(), end.isoformat(), leave_type,
             start_portion, end_portion, status),
        ).lastrowid
        conn.commit()
    return leave_id


def _week(client, venue, monday):
    return client.get(f"/v/{venue['slug']}/rota/?week={monday.isoformat()}").data.decode("utf-8")


# --------------------------------------------------------------------------- #
# The week grid
# --------------------------------------------------------------------------- #

def test_a_shift_on_a_leave_day_is_flagged_on_the_grid(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Holly Day")
    monday = _next_monday()
    _shift(app, venue, person_id, monday)
    _leave(app, venue, person_id, monday, monday + timedelta(days=2))
    login_as_pub(client, venue["pub_id"])

    html = _week(client, venue, monday)

    assert "shift-chip shift-chip-on-leave" in html
    assert 'leave-badge leave-badge-paid" title="On approved leave this day: Paid leave">Hol</span>' in html
    assert "1 shift is on a day that person has approved leave." in html
    # The shift is still there: it is flagged, not removed.
    assert "10:30-17:00" in html


def test_the_flag_names_the_kind_of_leave(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Poorly Pat")
    monday = _next_monday()
    _shift(app, venue, person_id, monday)
    _leave(app, venue, person_id, monday, monday, leave_type="sick")
    login_as_pub(client, venue["pub_id"])

    assert ">Sick</span>" in _week(client, venue, monday)


def test_no_flag_without_leave_or_for_leave_that_is_only_requested(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Working Wendy")
    monday = _next_monday()
    _shift(app, venue, person_id, monday)
    _leave(app, venue, person_id, monday, monday, status="pending")
    login_as_pub(client, venue["pub_id"])

    html = _week(client, venue, monday)

    assert "shift-chip-on-leave" not in html
    assert "has approved leave" not in html


def test_staff_full_rota_never_shows_the_flag(app, client, venue):
    """Colleagues must not read somebody's leave type off a shift chip."""
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Poorly Pat")
    monday = _next_monday()
    _shift(app, venue, person_id, monday)
    _leave(app, venue, person_id, monday, monday, leave_type="sick")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("INSERT INTO week_notification (venue_id, week_start_date) VALUES (?, ?)",
                     (venue["id"], monday.isoformat()))
        conn.commit()
    login_as_person(client, person_id)

    html = client.get(f"/v/{venue['slug']}/staff/rota?week={monday.isoformat()}").data.decode("utf-8")

    assert "shift-chip-on-leave" not in html
    assert "Sick" not in html


# --------------------------------------------------------------------------- #
# Half days
# --------------------------------------------------------------------------- #

def _row(**kw):
    base = {"start_portion": "full", "end_portion": "full"}
    base.update(kw)
    return base


def test_a_morning_shift_is_fine_when_leave_starts_at_midday():
    leave = _row(start_date="2026-11-23", end_date="2026-11-25", start_portion="half")
    morning = {"shift_date": "2026-11-23", "start_time": "09:00", "end_time": "12:00"}
    afternoon = {"shift_date": "2026-11-23", "start_time": "12:00", "end_time": "17:00"}
    overnight = {"shift_date": "2026-11-23", "start_time": "20:00", "end_time": "02:00"}

    assert shift_clashes_with_leave(morning, leave) is False
    assert shift_clashes_with_leave(afternoon, leave) is True
    assert shift_clashes_with_leave(overnight, leave) is True


def test_an_afternoon_shift_is_fine_when_leave_ends_at_midday():
    leave = _row(start_date="2026-11-23", end_date="2026-11-25", end_portion="half")
    morning = {"shift_date": "2026-11-25", "start_time": "09:00", "end_time": "13:00"}
    afternoon = {"shift_date": "2026-11-25", "start_time": "12:00", "end_time": "17:00"}
    middle = {"shift_date": "2026-11-24", "start_time": "12:00", "end_time": "17:00"}

    assert shift_clashes_with_leave(morning, leave) is True
    assert shift_clashes_with_leave(afternoon, leave) is False
    assert shift_clashes_with_leave(middle, leave) is True


def test_a_single_half_day_flags_any_shift_that_day():
    """It doesn't say which half, so warn rather than guess."""
    leave = _row(start_date="2026-11-23", end_date="2026-11-23", start_portion="half")
    morning = {"shift_date": "2026-11-23", "start_time": "09:00", "end_time": "12:00"}

    assert shift_clashes_with_leave(morning, leave) is True


# --------------------------------------------------------------------------- #
# The leave screen
# --------------------------------------------------------------------------- #

def test_a_pending_request_shows_the_shifts_inside_it(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Holly Day")
    monday = _next_monday()
    _shift(app, venue, person_id, monday)
    _shift(app, venue, person_id, monday + timedelta(days=9))  # outside the request
    _leave(app, venue, person_id, monday, monday + timedelta(days=2), status="pending")
    login_as_pub(client, venue["pub_id"])

    html = client.get(f"/v/{venue['slug']}/rota/leave").data.decode("utf-8")

    assert "On the rota in these dates:" in html
    assert f"Mon {monday.day} {monday.strftime('%B')} (10:30-17:00)." in html
    assert f"week={monday.isoformat()}" in html


def test_approving_says_they_are_still_on_the_rota_and_keeps_the_shift(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Holly Day")
    monday = _next_monday()
    shift_id = _shift(app, venue, person_id, monday)
    leave_id = _leave(app, venue, person_id, monday, monday, status="pending")
    login_as_pub(client, venue["pub_id"])

    html = client.post(f"/v/{venue['slug']}/rota/leave/{leave_id}/approve",
                       follow_redirects=True).data.decode("utf-8")

    assert "Holly Day is still on the rota during this leave:" in html
    assert "Still on the rota:" in html
    with app.app_context():
        assert db_module.get_db().execute(
            "SELECT status FROM shift WHERE id = ?", (shift_id,)).fetchone()["status"] == "scheduled"


def test_approving_leave_with_no_shifts_says_nothing_about_the_rota(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Holly Day")
    monday = _next_monday()
    leave_id = _leave(app, venue, person_id, monday, monday, status="pending")
    login_as_pub(client, venue["pub_id"])

    html = client.post(f"/v/{venue['slug']}/rota/leave/{leave_id}/approve",
                       follow_redirects=True).data.decode("utf-8")

    assert "still on the rota" not in html.lower()


def test_adding_leave_by_hand_warns_too(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Holly Day")
    monday = _next_monday()
    _shift(app, venue, person_id, monday)
    login_as_pub(client, venue["pub_id"])

    html = client.post(
        f"/v/{venue['slug']}/rota/leave/create",
        data={"person_id": person_id, "start_date": monday.isoformat(),
              "end_date": monday.isoformat(), "leave_type": "paid"},
        follow_redirects=True,
    ).data.decode("utf-8")

    assert "Leave added." in html
    assert "Holly Day is still on the rota during this leave:" in html


def test_leave_for_everyone_counts_the_shifts_left_standing(app, client, venue):
    first, _m, _e = create_active_staff(app, venue["id"], name="Holly Day")
    second, _m2, _e2 = create_active_staff(app, venue["id"], name="Noel Knight")
    monday = _next_monday()
    _shift(app, venue, first, monday)
    _shift(app, venue, second, monday + timedelta(days=1))
    login_as_pub(client, venue["pub_id"])

    html = client.post(
        f"/v/{venue['slug']}/rota/leave/bulk",
        data={"start_date": monday.isoformat(), "end_date": (monday + timedelta(days=6)).isoformat(),
              "leave_type": "paid"},
        follow_redirects=True,
    ).data.decode("utf-8")

    assert "2 shifts are still on the rota in those dates." in html


def test_a_shift_already_worked_is_not_reported_as_needing_cover(app, client, venue):
    """Sick leave recorded afterwards over days that included a past shift."""
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Poorly Pat")
    yesterday = uk_today() - timedelta(days=1)
    _shift(app, venue, person_id, yesterday)
    login_as_pub(client, venue["pub_id"])

    html = client.post(
        f"/v/{venue['slug']}/rota/leave/create",
        data={"person_id": person_id, "start_date": yesterday.isoformat(),
              "end_date": uk_today().isoformat(), "leave_type": "sick"},
        follow_redirects=True,
    ).data.decode("utf-8")

    assert "still on the rota" not in html.lower()


# --------------------------------------------------------------------------- #
# Weekly hours under the name
# --------------------------------------------------------------------------- #

def test_rostered_hours_for_the_week_show_under_the_name(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Kelly-Ann")
    monday = _next_monday()
    _shift(app, venue, person_id, monday, "10:30", "17:00")                       # 6.5
    _shift(app, venue, person_id, monday + timedelta(days=2), "20:00", "02:00")   # 6, past midnight
    _shift(app, venue, person_id, monday + timedelta(days=7), "09:00", "17:00")   # next week
    login_as_pub(client, venue["pub_id"])

    html = _week(client, venue, monday)

    assert ">12.5 hrs</span>" in html


def test_one_hour_is_singular(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Brief Bea")
    monday = _next_monday()
    _shift(app, venue, person_id, monday, "09:00", "10:00")
    login_as_pub(client, venue["pub_id"])

    assert ">1 hr</span>" in _week(client, venue, monday)


def test_nobody_with_no_shifts_gets_an_hours_line(app, client, venue):
    """Owner's call, 2026-11-19: show nothing rather than "0 hrs"."""
    create_active_staff(app, venue["id"], name="Jane Howard")
    login_as_pub(client, venue["pub_id"])

    html = _week(client, venue, _next_monday())

    assert "Jane Howard" in html
    assert "rota-week-hours" not in html
