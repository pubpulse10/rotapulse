"""
Holiday allowances and the per-person position.

Step 2 of docs/leave-design.md. Statutory holiday is 5.6 WEEKS, not 28 days,
so an allowance pro-rates by working pattern: a flat 28 for everybody would
give a two-day-a-week cleaner roughly two and a half times what her pattern
earns. The owner asked for "28 for everyone" and agreed to this instead.

The position is allowance + carried over, minus taken and booked ahead. Taken
and booked are separate on purpose: "I have had 15 days" and "I have committed
19 of my 28" are different conversations.
"""

from datetime import date, timedelta

import pytest

from app import db as db_module
from app.leave import (allowance_for, calculated_allowance, holiday_year_bounds, position,
                       prorata_for_starter, statutory_minimum_days)
from tests.conftest import create_active_staff, login_as_person, login_as_pub

FIVE_DAYS = '{"mon":true,"tue":true,"wed":true,"thu":true,"fri":true,"sat":false,"sun":false}'
TWO_DAYS = '{"mon":true,"tue":false,"wed":false,"thu":false,"fri":true,"sat":false,"sun":false}'
TODAY = date(2026, 6, 30)  # mid-way through a 1 January holiday year


def _staff(app, venue, name="Allowance Tester", availability=FIVE_DAYS, **detail):
    person_id, membership_id, _e = create_active_staff(app, venue["id"], name=name)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE rota_staff_detail SET availability = ? WHERE venue_membership_id = ?",
                     (availability, membership_id))
        for column, value in detail.items():
            conn.execute(f"UPDATE rota_staff_detail SET {column} = ? WHERE venue_membership_id = ?",
                         (value, membership_id))
        conn.execute("UPDATE venue_settings SET holiday_year_start_date = '01-01' WHERE venue_id = ?",
                     (venue["id"],))
        conn.commit()
    return person_id, membership_id


def _rows(app, venue, membership_id):
    conn = db_module.get_db()
    detail = conn.execute("SELECT * FROM rota_staff_detail WHERE venue_membership_id = ?",
                          (membership_id,)).fetchone()
    settings = conn.execute("SELECT * FROM venue_settings WHERE venue_id = ?", (venue["id"],)).fetchone()
    return detail, settings


def _position(app, venue, person_id, membership_id, today=TODAY):
    with app.app_context():
        detail, settings = _rows(app, venue, membership_id)
        return position(db_module.get_db(), person_id, membership_id, detail, settings, today=today)


def _leave(app, venue, person_id, start, end, leave_type="paid", days=None):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            """INSERT INTO leave_request (person_id, venue_id, start_date, end_date, leave_type, status, days_counted)
               VALUES (?, ?, ?, ?, ?, 'approved', ?)""",
            (person_id, venue["id"], start, end, leave_type, days),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# The allowance rule
# --------------------------------------------------------------------------- #

def test_a_full_time_pattern_gets_the_whole_venue_allowance():
    assert calculated_allowance(28, 5, 5) == 28.0


def test_a_part_timer_gets_a_share_not_the_whole_thing():
    """The headline decision. A flat 28 would give somebody working two days a
    week about two and a half times the 11.2 days their pattern earns."""
    assert calculated_allowance(28, 5, 2) == 11.5
    assert calculated_allowance(28, 5, 2) != 28


@pytest.mark.parametrize("days_per_week", [1, 2, 3, 4, 5])
def test_the_allowance_is_never_below_the_statutory_minimum(days_per_week):
    """Rounding is always UP to the next half day: one day a week earns 5.6
    days, and rounding that down to 5.5 would be short."""
    allowance = calculated_allowance(28, 5, days_per_week)
    assert allowance >= statutory_minimum_days(days_per_week)


def test_a_generous_venue_pro_rates_from_its_own_number():
    assert calculated_allowance(30, 5, 3) == 18.0


def test_the_cap_is_the_venues_own_full_time_figure():
    """Somebody down as working seven days a week doesn't earn more than a
    full-time year."""
    assert calculated_allowance(28, 5, 7) == 28.0


def test_a_mid_year_starter_gets_the_share_of_the_year_they_are_here_for():
    year_start, year_end = date(2026, 1, 1), date(2026, 12, 31)
    assert prorata_for_starter(28.0, "2026-07-01", year_start, year_end) == 14.5  # half a year, rounded up
    assert prorata_for_starter(28.0, "2026-01-01", year_start, year_end) == 28.0  # started at the year start
    assert prorata_for_starter(28.0, "2025-06-01", year_start, year_end) == 28.0  # already here


def test_holiday_year_bounds_run_to_the_day_before_the_next_one():
    assert holiday_year_bounds("01-01", date(2026, 6, 30)) == (date(2026, 1, 1), date(2026, 12, 31))
    assert holiday_year_bounds("04-06", date(2026, 6, 30)) == (date(2026, 4, 6), date(2027, 4, 5))
    # A 29 February start has no 29 February next year.
    assert holiday_year_bounds("02-29", date(2024, 6, 30)) == (date(2024, 2, 29), date(2025, 2, 27))


# --------------------------------------------------------------------------- #
# Where the figure comes from
# --------------------------------------------------------------------------- #

def test_an_allowance_typed_by_hand_is_never_recalculated_over(app, venue):
    person_id, membership_id = _staff(app, venue, availability=TWO_DAYS, allowance_days=20)

    with app.app_context():
        detail, settings = _rows(app, venue, membership_id)
        allowance = allowance_for(detail, settings, date(2026, 1, 1), date(2026, 12, 31))

    assert allowance["days"] == 20.0
    assert allowance["source"] == "manual"


def test_an_allowance_below_the_statutory_minimum_is_flagged(app, client, venue):
    person_id, membership_id = _staff(app, venue, availability=FIVE_DAYS, allowance_days=5)

    with app.app_context():
        detail, settings = _rows(app, venue, membership_id)
        allowance = allowance_for(detail, settings, date(2026, 1, 1), date(2026, 12, 31))
    assert allowance["below_statutory"] is True

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/admin/staff/{membership_id}/edit")
    assert b"below the statutory minimum" in resp.data


def test_an_allowance_cannot_be_worked_out_without_a_working_pattern(app, client, venue):
    person_id, membership_id = _staff(app, venue, availability=None)

    with app.app_context():
        detail, settings = _rows(app, venue, membership_id)
        allowance = allowance_for(detail, settings, date(2026, 1, 1), date(2026, 12, 31))

    assert allowance["days"] is None
    assert allowance["source"] == "unknown"

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/admin/staff/{membership_id}/edit")
    assert b"Tick the days they normally work" in resp.data


def test_the_availability_boxes_are_not_ticked_by_default_when_unknown(app, client, venue):
    """They used to default to ticked, which turned "we don't know" into "works
    every day" the moment anybody saved the form — and a 7-day week is what made
    a week off count as 7 days of holiday."""
    person_id, membership_id = _staff(app, venue, availability=None)
    login_as_pub(client, venue["pub_id"])

    resp = client.get(f"/v/{venue['slug']}/admin/staff/{membership_id}/edit")

    assert b"Not set yet" in resp.data
    assert b'name="available_sun" checked' not in resp.data


# --------------------------------------------------------------------------- #
# The position
# --------------------------------------------------------------------------- #

def test_taken_and_booked_ahead_are_counted_separately(app, venue):
    person_id, membership_id = _staff(app, venue)
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06")   # taken, a full week
    _leave(app, venue, person_id, "2026-09-07", "2026-09-11")   # booked ahead

    pos = _position(app, venue, person_id, membership_id)

    assert pos["taken"] == 5.0
    assert pos["booked"] == 5.0
    assert pos["total"] == 28.0
    assert pos["remaining"] == 18.0


def test_a_booking_that_straddles_today_splits_between_taken_and_booked(app, venue):
    """Counted live for each side rather than using the frozen total, which
    covers the whole booking and belongs to neither half."""
    person_id, membership_id = _staff(app, venue)
    _leave(app, venue, person_id, "2026-06-29", "2026-07-03", days=5.0)  # Mon to Fri, today is Tuesday

    pos = _position(app, venue, person_id, membership_id)

    assert pos["taken"] == 2.0   # Monday and Tuesday
    assert pos["booked"] == 3.0  # Wednesday to Friday
    assert pos["remaining"] == 23.0


def test_carried_over_days_are_added_to_the_allowance(app, venue):
    person_id, membership_id = _staff(app, venue)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("INSERT INTO leave_carry_over (venue_membership_id, year_start_date, days) VALUES (?, '2026-01-01', 3)",
                     (membership_id,))
        conn.commit()

    pos = _position(app, venue, person_id, membership_id)

    assert pos["carried_over"] == 3.0
    assert pos["total"] == 31.0
    assert pos["remaining"] == 31.0


@pytest.mark.parametrize("leave_type", ["unpaid", "sick", "maternity", "lieu"])
def test_only_paid_leave_changes_the_balance(app, venue, leave_type):
    person_id, membership_id = _staff(app, venue, name=f"Balance {leave_type}")
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06", leave_type=leave_type)

    pos = _position(app, venue, person_id, membership_id)

    assert pos["remaining"] == 28.0          # untouched
    assert pos["by_type"][leave_type] == 5.0  # but recorded and reported


def test_the_position_cannot_be_worked_out_without_a_working_pattern(app, venue):
    person_id, membership_id = _staff(app, venue, availability=None)

    pos = _position(app, venue, person_id, membership_id)

    assert pos["countable"] is False
    assert pos["taken"] is None


# --------------------------------------------------------------------------- #
# The screens
# --------------------------------------------------------------------------- #

def test_the_venue_sets_what_a_full_time_year_is_worth(app, client, venue):
    login_as_pub(client, venue["pub_id"])

    client.post(f"/v/{venue['slug']}/admin/settings", data={
        "venue_name": "Test Venue", "full_time_allowance_days": "30",
        "full_time_days_per_week": "5", "pay_period_type": "weekly",
    }, follow_redirects=True)

    with app.app_context():
        row = db_module.get_db().execute(
            "SELECT * FROM venue_settings WHERE venue_id = ?", (venue["id"],)).fetchone()
    assert row["full_time_allowance_days"] == 30


def test_carry_over_is_saved_against_the_holiday_year_and_can_be_cleared(app, client, venue):
    person_id, membership_id = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])
    form = {"name": "Allowance Tester", "carried_over_days": "3.5", "available_mon": "on", "available_tue": "on"}

    client.post(f"/v/{venue['slug']}/admin/staff/{membership_id}/edit", data=form, follow_redirects=True)
    with app.app_context():
        rows = db_module.get_db().execute(
            "SELECT * FROM leave_carry_over WHERE venue_membership_id = ?", (membership_id,)).fetchall()
    assert len(rows) == 1 and rows[0]["days"] == 3.5

    # Clearing the box removes it rather than leaving a stale figure behind.
    client.post(f"/v/{venue['slug']}/admin/staff/{membership_id}/edit",
                data={**form, "carried_over_days": ""}, follow_redirects=True)
    with app.app_context():
        rows = db_module.get_db().execute(
            "SELECT * FROM leave_carry_over WHERE venue_membership_id = ?", (membership_id,)).fetchall()
    assert rows == []


def test_the_allowance_and_hours_boxes_save_and_a_blank_means_work_it_out(app, client, venue):
    person_id, membership_id = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])
    form = {"name": "Allowance Tester", "available_mon": "on", "available_tue": "on",
            "allowance_days": "22", "usual_daily_hours": "6.5", "holiday_pay_rolled_up": "on"}

    client.post(f"/v/{venue['slug']}/admin/staff/{membership_id}/edit", data=form, follow_redirects=True)
    with app.app_context():
        detail = db_module.get_db().execute(
            "SELECT * FROM rota_staff_detail WHERE venue_membership_id = ?", (membership_id,)).fetchone()
    assert detail["allowance_days"] == 22
    assert detail["usual_daily_hours"] == 6.5
    assert detail["holiday_pay_rolled_up"] == 1

    client.post(f"/v/{venue['slug']}/admin/staff/{membership_id}/edit",
                data={**form, "allowance_days": "", "usual_daily_hours": "", "holiday_pay_rolled_up": ""},
                follow_redirects=True)
    with app.app_context():
        detail = db_module.get_db().execute(
            "SELECT * FROM rota_staff_detail WHERE venue_membership_id = ?", (membership_id,)).fetchone()
    assert detail["allowance_days"] is None
    assert detail["usual_daily_hours"] is None
    assert detail["holiday_pay_rolled_up"] == 0


def test_a_staff_member_sees_their_own_position(app, client, venue):
    person_id, membership_id = _staff(app, venue, name="Sees Own")
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06")
    login_as_person(client, person_id)

    resp = client.get(f"/v/{venue['slug']}/staff/leave")

    assert resp.status_code == 200
    assert b"Your allowance" in resp.data
    assert b"Taken so far" in resp.data
    assert b"to use before" in resp.data
