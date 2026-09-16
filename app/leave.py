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

import calendar
import json
import math
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


def leave_days_in_window(db, person_id: int, availability_json: str,
                        window_start: date, window_end: date, types=("paid",)):
    """Days of leave of the given types falling inside a window.

    One counter behind every figure on the position screen and the staff page,
    so "taken", "booked ahead" and the by-type breakdown cannot disagree with
    each other about what a day is.

    None means their availability isn't set, so their days cannot be counted
    at all. Callers must say so rather than show a nought.
    """
    if working_pattern(availability_json) is None:
        return None
    if window_end < window_start:
        return 0.0

    placeholders = ",".join("?" for _ in types)
    rows = db.execute(
        f"""SELECT start_date, end_date, start_portion, end_portion, days_counted
            FROM leave_request
            WHERE person_id = ? AND status = 'approved' AND leave_type IN ({placeholders})
            AND end_date >= ? AND start_date <= ?""",
        (person_id, *types, window_start.isoformat(), window_end.isoformat()),
    ).fetchall()

    total = 0.0
    for row in rows:
        booked_start = date.fromisoformat(row["start_date"])
        booked_end = date.fromisoformat(row["end_date"])
        start = max(booked_start, window_start)
        end = min(booked_end, window_end)
        if end < start:
            continue

        # The frozen figure covers the whole booking, so it can only be used
        # when the whole booking falls inside the window being counted. One
        # that straddles the window's edge is counted live for the part inside
        # it — a half day at an end outside the window is not this window's.
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


def days_taken_count(db, person_id: int, availability_json: str, year_start_mmdd: str, today=None):
    """Paid leave a person has used so far this holiday year.

    Paid only: the other four types are recorded and reported, but none of
    them comes off a holiday balance (docs/leave-design.md). Stops at today —
    leave booked for next month has not been taken yet; position() below
    reports that separately.
    """
    today = today or uk_today()
    year_start = _current_holiday_year_start(year_start_mmdd, today)
    return leave_days_in_window(db, person_id, availability_json, year_start, today, ("paid",))


# --------------------------------------------------------------------------- #
# Allowances (step 2 of docs/leave-design.md)
# --------------------------------------------------------------------------- #

def holiday_year_bounds(year_start_mmdd: str, today=None):
    """First and last day of the holiday year that today falls in."""
    today = today or uk_today()
    start = _current_holiday_year_start(year_start_mmdd, today)
    # A 29 February start has no 29 February next year: fall back to the last
    # day of that month, the same rule pay_periods.py uses for a month end.
    last_day = calendar.monthrange(start.year + 1, start.month)[1]
    next_start = date(start.year + 1, start.month, min(start.day, last_day))
    return start, next_start - timedelta(days=1)


def usual_days_per_week(availability_json):
    """How many days a week this person normally works, or None if unknown."""
    pattern = working_pattern(availability_json)
    if pattern is None:
        return None
    return sum(1 for worked in pattern.values() if worked)


def _round_up_to_half(value: float) -> float:
    """Always UP to the next half day. Rounding an allowance down can put it
    below the statutory minimum — one day a week is 5.6 days, and 5.5 would be
    short. Half a day in the staff member's favour is the cheap side to err on.
    """
    return math.ceil(value * 2) / 2


def statutory_minimum_days(days_per_week):
    """5.6 weeks, capped at 28 days — the statutory minimum for that pattern.

    The author's understanding of the Working Time Regulations, not legal
    advice; see the note at the end of docs/leave-design.md.
    """
    if not days_per_week:
        return None
    return round(min(5.6 * days_per_week, 28.0), 2)


def calculated_allowance(full_time_allowance, full_time_days_per_week, days_per_week):
    """The venue's full-time allowance, pro-rated by working pattern.

    With the 28/5 defaults this is exactly 5.6 weeks. It also generalises: a
    venue giving 30 days to full-timers pro-rates correctly for everyone else.
    """
    if not days_per_week or not full_time_days_per_week or not full_time_allowance:
        return None
    share = float(full_time_allowance) * (days_per_week / float(full_time_days_per_week))
    return _round_up_to_half(min(share, float(full_time_allowance)))


def prorata_for_starter(allowance, start_date, year_start: date, year_end: date):
    """Somebody who started part-way through the holiday year gets the share
    of it they are actually here for."""
    if not start_date or allowance is None:
        return allowance
    try:
        started = date.fromisoformat(start_date)
    except (ValueError, TypeError):
        return allowance
    if started <= year_start:
        return allowance
    if started > year_end:
        return 0.0
    days_here = (year_end - started).days + 1
    days_in_year = (year_end - year_start).days + 1
    return _round_up_to_half(allowance * days_here / days_in_year)


def allowance_for(detail, settings, year_start: date, year_end: date):
    """What this person's holiday allowance is, and where the figure came from.

    A number on their record is one the landlord typed and is never
    recalculated over the top of; NULL means work it out. Returns `days` of
    None when availability isn't set, because then there is no pattern to
    pro-rate by and a guess would be worse than saying so.
    """
    full_time = (settings["full_time_allowance_days"] if settings else None) or 28
    full_time_week = (settings["full_time_days_per_week"] if settings else None) or 5
    availability = detail["availability"] if detail else None
    per_week = usual_days_per_week(availability)
    statutory = statutory_minimum_days(per_week)

    manual = detail["allowance_days"] if detail else None
    if manual is not None:
        days = float(manual)
        source = "manual"
        prorata_from = None
    else:
        calculated = calculated_allowance(full_time, full_time_week, per_week)
        prorata_from = None
        if calculated is not None and detail and detail["start_date"]:
            after = prorata_for_starter(calculated, detail["start_date"], year_start, year_end)
            if after != calculated:
                prorata_from = detail["start_date"]
                calculated = after
        days = calculated
        source = "calculated" if calculated is not None else "unknown"

    return {
        "days": days,
        "source": source,
        "days_per_week": per_week,
        "prorata_from": prorata_from,
        "statutory_minimum": statutory,
        # Being more generous is always the landlord's call. Being accidentally
        # under is the one that causes trouble.
        "below_statutory": bool(days is not None and statutory is not None and days < statutory),
    }


def carried_over_days(db, membership_id: int, year_start: date) -> float:
    row = db.execute(
        "SELECT days FROM leave_carry_over WHERE venue_membership_id = ? AND year_start_date = ?",
        (membership_id, year_start.isoformat()),
    ).fetchone()
    return float(row["days"]) if row else 0.0


def position(db, person_id: int, membership_id: int, detail, settings, today=None):
    """One staff member's holiday position for the holiday year they are in.

    allowance + carried over = total; minus what they have taken and what they
    have booked ahead, leaving what there is left to book. Taken and booked are
    separate on purpose: "I have had 15 days" and "I have committed 19 of my
    28" are different conversations, and showing only the first is how people
    end up surprised in December.
    """
    today = today or uk_today()
    year_start_mmdd = settings["holiday_year_start_date"] if settings else None
    year_start, year_end = holiday_year_bounds(year_start_mmdd, today)
    availability = detail["availability"] if detail else None

    allowance = allowance_for(detail, settings, year_start, year_end)
    carried = carried_over_days(db, membership_id, year_start)

    taken = leave_days_in_window(db, person_id, availability, year_start, min(today, year_end), ("paid",))
    booked = leave_days_in_window(db, person_id, availability, max(today + timedelta(days=1), year_start), year_end, ("paid",))
    by_type = {
        key: leave_days_in_window(db, person_id, availability, year_start, year_end, (key,))
        for key in LEAVE_TYPE_LABELS
    }

    total = None if allowance["days"] is None else round(allowance["days"] + carried, 2)
    remaining = None
    if total is not None and taken is not None and booked is not None:
        remaining = round(total - taken - booked, 2)

    return {
        "year_start": year_start,
        "year_end": year_end,
        "allowance": allowance,
        "carried_over": carried,
        "total": total,
        "taken": taken,
        "booked": booked,
        "remaining": remaining,
        "by_type": by_type,
        # Nothing can be counted at all without a working pattern.
        "countable": taken is not None,
    }


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
