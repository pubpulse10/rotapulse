"""
Leave: the types, how much of it a person has used, and telling them the
answer when their request is decided.

See docs/leave-design.md for the whole design and what is deliberately not
built yet. The parts that matter here:

* Five types, of which only PAID leave comes off a holiday balance. The other
  four are recorded so the rota knows the person is away and so the leave
  report can show them, but they never reduce anyone's entitlement.

* A "day" is defined relative to the person (spec §8): a date inside approved
  leave counts only when they would normally work that weekday, per their own
  availability pattern. A day they never work anyway is not a day of holiday.
  Either end of a booking can be a half day.

* The days and hours a leave record is worth are frozen onto the record when
  it is approved (freeze_counts). Recomputing them on every read would mean
  that editing somebody's availability, or their usual daily hours, silently
  rewrote last year's history — and this is a number staff argue about.
  Records approved before those columns existed carry no frozen figures, so
  they are still counted live, which is why count_days() is still used below.

* Reset boundary: a landlord-defined year-start date per venue (MM-DD, not a
  fixed calendar year) — mirrors the pay-period settings' own pattern.
"""

import json
from datetime import date, timedelta

from app.date_format import format_uk_date
from app.notifications import send_email, send_sms
from app.uk_time import uk_today

WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# key, label, may a staff member request it themselves, does it use up holiday
LEAVE_TYPES = [
    ("paid", "Paid leave", True, True),
    ("unpaid", "Unpaid leave", True, False),
    ("sick", "Sick", False, False),
    ("maternity", "Maternity", False, False),
    ("lieu", "Day in lieu", False, False),
]
LEAVE_TYPE_KEYS = {key for key, _label, _requestable, _allowance in LEAVE_TYPES}
LEAVE_TYPE_LABELS = {key: label for key, label, _requestable, _allowance in LEAVE_TYPES}
# Sick, maternity and lieu are admin-recorded: nobody requests being ill in
# advance, and the other two are decisions the landlord makes, not requests.
STAFF_REQUESTABLE_TYPES = {key for key, _l, requestable, _a in LEAVE_TYPES if requestable}
ALLOWANCE_TYPES = {key for key, _l, _r, uses_allowance in LEAVE_TYPES if uses_allowance}

PORTIONS = {"full", "half"}

# How far back to look when working out what one of a person's days is worth
# in hours, for staff who have no figure on their record yet.
USUAL_HOURS_LOOKBACK_WEEKS = 12


def _current_holiday_year_start(year_start_mmdd: str, today: date) -> date:
    """Falls back to 1 Jan for anything that isn't a clean MM-DD, rather
    than raising. The settings form now saves this via two validated
    day/month dropdowns (admin_config.py), so a malformed value shouldn't
    get saved again — but this stays defensive for whatever's already
    stored from before that existed (a real "0101" instead of "01-01" once
    crashed every staff member's leave page at that venue)."""
    if year_start_mmdd:
        try:
            month, day = map(int, year_start_mmdd.split("-"))
            candidate = date(today.year, month, day)
            if candidate > today:
                candidate = date(today.year - 1, month, day)
            return candidate
        except (ValueError, TypeError):
            pass
    return date(today.year, 1, 1)


def working_pattern(availability_json):
    """The person's working days, or None when we genuinely don't know.

    2026-09-16: this used to be `availability.get(weekday, True)` — a missing
    day, or no availability at all, was read as "works that day". Somebody
    whose availability had never been set therefore had a week's holiday
    counted as 7 days instead of 5. Nobody noticed while the figure was
    informational; it becomes an argument with a staff member the moment it
    is "allowance minus taken equals remaining".

    An all-false pattern is treated as unknown too. It cannot be a real
    working week, and counting every holiday as nought days would be the same
    silent wrongness in the opposite direction.
    """
    if not availability_json:
        return None
    try:
        pattern = json.loads(availability_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(pattern, dict):
        return None
    days = {key: bool(pattern.get(key)) for key in WEEKDAY_KEYS}
    if not any(days.values()):
        return None
    return days


def count_days(availability_json, start_date: str, end_date: str,
               start_portion: str = "full", end_portion: str = "full"):
    """How many of a person's own working days a booking uses up.

    None when their availability isn't known — the caller is expected to say
    so, rather than print a number that isn't true.
    """
    pattern = working_pattern(availability_json)
    if pattern is None:
        return None

    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        return 0.0

    working = []
    day = start
    while day <= end:
        if pattern[WEEKDAY_KEYS[day.weekday()]]:
            working.append(day)
        day += timedelta(days=1)

    total = float(len(working))
    if not total:
        return 0.0

    # A single-day booking can only lose half a day once, however the two
    # portions are set. A longer one loses half at each end that asks for it.
    if start == end:
        if "half" in (start_portion, end_portion):
            total -= 0.5
    else:
        if start_portion == "half" and start in working:
            total -= 0.5
        if end_portion == "half" and end in working:
            total -= 0.5
    return round(total, 2)


def usual_daily_hours(db, person_id: int, venue_id: int, stored=None):
    """What one of this person's days is worth in hours.

    The figure on their staff record wins. Until the screen for editing it
    exists (step 2 of docs/leave-design.md), fall back to the average length
    of the shifts they have actually clocked recently, so leave records carry
    an hours value from the very first version — which is the whole point of
    the "store hours from day one" decision in the design.

    None when there is nothing to go on: better an honest blank than a number
    somebody might pay against.
    """
    if stored:
        return float(stored)

    since = (uk_today() - timedelta(weeks=USUAL_HOURS_LOOKBACK_WEEKS)).isoformat()
    row = db.execute(
        """SELECT AVG((julianday(attendance.clock_out_at) - julianday(attendance.clock_in_at)) * 24.0) AS avg_hours
           FROM attendance
           JOIN shift ON shift.id = attendance.shift_id
           WHERE shift.person_id = ? AND shift.venue_id = ? AND shift.shift_date >= ?
           AND attendance.clock_in_at IS NOT NULL AND attendance.clock_out_at IS NOT NULL
           AND (attendance.approval_status IS NULL OR attendance.approval_status != 'rejected')""",
        (person_id, venue_id, since),
    ).fetchone()
    if row is None or row["avg_hours"] is None:
        return None
    return round(float(row["avg_hours"]), 2)


def freeze_counts(db, leave_id: int) -> None:
    """Work out what a leave record is worth and write it onto the record.

    Called when leave is approved, or created already-approved by an admin —
    never on read. Does not commit; the caller's own commit covers it.
    """
    row = db.execute(
        """SELECT leave_request.*, rota_staff_detail.availability, rota_staff_detail.usual_daily_hours
           FROM leave_request
           JOIN venue_membership ON venue_membership.person_id = leave_request.person_id
               AND venue_membership.venue_id = leave_request.venue_id
           LEFT JOIN rota_staff_detail ON rota_staff_detail.venue_membership_id = venue_membership.id
           WHERE leave_request.id = ?""",
        (leave_id,),
    ).fetchone()
    if row is None:
        return

    days = count_days(
        row["availability"], row["start_date"], row["end_date"],
        row["start_portion"] or "full", row["end_portion"] or "full",
    )
    hours = None
    if days is not None:
        per_day = usual_daily_hours(db, row["person_id"], row["venue_id"], row["usual_daily_hours"])
        if per_day is not None:
            hours = round(days * per_day, 2)

    db.execute(
        "UPDATE leave_request SET days_counted = ?, hours_counted = ? WHERE id = ?",
        (days, hours, leave_id),
    )


def days_taken_count(db, person_id: int, availability_json: str, year_start_mmdd: str, today=None):
    """Paid leave a person has used so far this holiday year.

    Paid only: the other four types are recorded and reported, but none of
    them comes off a holiday balance (docs/leave-design.md).

    Still stops at today — leave booked for next month has not been taken.
    The per-person position screen in step 2 shows taken and booked ahead as
    separate figures, which is the honest version of this one number.

    None means their availability isn't set, so their days cannot be counted
    at all. Callers must say so rather than show a nought.
    """
    today = today or uk_today()
    year_start = _current_holiday_year_start(year_start_mmdd, today)
    if working_pattern(availability_json) is None:
        return None

    rows = db.execute(
        """SELECT start_date, end_date, start_portion, end_portion, days_counted
           FROM leave_request
           WHERE person_id = ? AND status = 'approved' AND leave_type = 'paid'
           AND end_date >= ?""",
        (person_id, year_start.isoformat()),
    ).fetchall()

    total = 0.0
    for row in rows:
        booked_start = date.fromisoformat(row["start_date"])
        booked_end = date.fromisoformat(row["end_date"])
        start = max(booked_start, year_start)
        end = min(booked_end, today)
        if end < start:
            continue

        # The frozen figure covers the whole booking, so it can only be used
        # when the whole booking falls inside the window being counted. One
        # that straddles the year start, or runs past today, is counted live
        # for the part that falls inside it.
        if start == booked_start and end == booked_end and row["days_counted"] is not None:
            total += float(row["days_counted"])
            continue

        counted = count_days(
            availability_json, start.isoformat(), end.isoformat(),
            (row["start_portion"] or "full") if start == booked_start else "full",
            (row["end_portion"] or "full") if end == booked_end else "full",
        )
        total += counted or 0.0
    return round(total, 2)


def describe_booking(row) -> str:
    """"3 August 2026 to 5 August 2026", with any half days spelled out.
    Plain ASCII: this text goes out by SMS as well as email."""
    start, end = row["start_date"], row["end_date"]
    start_half = (row["start_portion"] or "full") == "half"
    end_half = (row["end_portion"] or "full") == "half"
    if start == end:
        return format_uk_date(start) + (" (half day)" if start_half or end_half else "")
    first = format_uk_date(start) + (" (from midday)" if start_half else "")
    last = format_uk_date(end) + (" (until midday)" if end_half else "")
    return f"{first} to {last}"


def notify_decision(db, venue, leave_row, decision: str) -> None:
    """Tell the staff member their leave was approved, declined or cancelled.

    Before this, the app notified ADMINS when a request came in and told the
    person who made it nothing at all — they had to go and look.

    Deliberately not part of the owner-configurable notification_setting
    system, which is about which admins hear about problems. Same shape as
    remind_staff_to_clock_in and admin_config._send_approval_message: straight
    to the person concerned, always on, by whichever channel they were
    invited through.

    Plain ASCII punctuation only: one character outside GSM-7 doubles the
    segment count of the whole text (see commit 6edf40d).
    """
    person = db.execute(
        """SELECT person.name, person.email, person.mobile, app_access.invite_method
           FROM person
           JOIN venue_membership ON venue_membership.person_id = person.id
               AND venue_membership.venue_id = ?
           LEFT JOIN app_access ON app_access.venue_membership_id = venue_membership.id
           WHERE person.id = ?
           LIMIT 1""",
        (leave_row["venue_id"], leave_row["person_id"]),
    ).fetchone()
    if person is None:
        return

    outcomes = {
        "approved": "has been approved. Enjoy it.",
        "declined": "has not been approved. Have a word with your manager if you need to know why.",
        "cancelled": "has been cancelled, so you are back on the rota for those dates. "
                     "Have a word with your manager if that is unexpected.",
    }
    outcome = outcomes.get(decision)
    if outcome is None:
        return

    type_label = LEAVE_TYPE_LABELS.get(leave_row["leave_type"] or "paid", "Leave").lower()
    message = (
        f"Hi {person['name']}, your {type_label} for {describe_booking(leave_row)} "
        f"at {venue['name']} {outcome}"
    )

    if person["invite_method"] == "sms" and person["mobile"]:
        send_sms(person["mobile"], message)
    elif person["email"]:
        send_email(person["email"], f"Leave {decision} - {venue['name']}", message)
