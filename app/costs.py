"""
Pure staff-cost calculation functions, shared between the day-cost figures
on the rota grid and the weekly/monthly cost dashboard (spec §10-11) — one
place computes cost, so the two can never silently disagree.

Both predicted and actual figures are always live reads of current data —
no "freeze"/snapshot step (spec §11): if the rota changes, predicted cost
updates immediately; if an attendance record is corrected, actual cost
updates immediately.
"""

from datetime import datetime

from app.db import get_db


def shift_hours(start_time: str, end_time: str) -> float:
    """Planned length of a shift in hours, including one running past midnight.

    A pub's late shift is routinely 20:00-02:00. Subtracting the clock times
    gives -18 for that, and the caller below used to clamp a negative to zero
    — so every overnight shift contributed exactly nothing to the predicted
    wage bill, silently and in the reassuring direction. actual_cost() never
    had the fault, because it works from full clock-in/clock-out timestamps,
    so the predicted and actual figures simply disagreed with nothing on
    screen to explain why.

    Equal times mean zero, not 24 hours: an ad-hoc shift is created with
    start_time == end_time as a placeholder (see staff_portal.start_ad_hoc_
    shift), and reading that as a full day would be a much bigger error than
    the one being fixed here.
    """
    start_h, start_m = map(int, start_time.split(":"))
    end_h, end_m = map(int, end_time.split(":"))
    minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
    if minutes < 0:
        minutes += 24 * 60
    return minutes / 60


def predicted_cost(venue_id: int, start_date: str, end_date: str) -> float:
    """From currently-rostered SHIFT data (planned hours x rate)."""
    db = get_db()
    rows = db.execute(
        """SELECT shift.start_time, shift.end_time, rota_staff_detail.hourly_pay_rate
           FROM shift
           JOIN venue_membership ON venue_membership.person_id = shift.person_id
               AND venue_membership.venue_id = shift.venue_id
           JOIN rota_staff_detail ON rota_staff_detail.venue_membership_id = venue_membership.id
           WHERE shift.venue_id = ? AND shift.status = 'scheduled'
           AND shift.shift_date BETWEEN ? AND ?""",
        (venue_id, start_date, end_date),
    ).fetchall()
    total = 0.0
    for row in rows:
        total += shift_hours(row["start_time"], row["end_time"]) * float(row["hourly_pay_rate"])
    return round(total, 2)


def actual_cost(venue_id: int, start_date: str, end_date: str) -> float:
    """From ATTENDANCE data (actual clocked hours x rate)."""
    db = get_db()
    rows = db.execute(
        """SELECT attendance.clock_in_at, attendance.clock_out_at, rota_staff_detail.hourly_pay_rate
           FROM attendance
           JOIN shift ON shift.id = attendance.shift_id
           JOIN venue_membership ON venue_membership.person_id = shift.person_id
               AND venue_membership.venue_id = shift.venue_id
           JOIN rota_staff_detail ON rota_staff_detail.venue_membership_id = venue_membership.id
           WHERE shift.venue_id = ? AND shift.shift_date BETWEEN ? AND ?
           AND attendance.clock_out_at IS NOT NULL
           AND (attendance.approval_status IS NULL OR attendance.approval_status != 'rejected')""",
        (venue_id, start_date, end_date),
    ).fetchall()
    total = 0.0
    for row in rows:
        clock_in = datetime.fromisoformat(row["clock_in_at"])
        clock_out = datetime.fromisoformat(row["clock_out_at"])
        hours = max((clock_out - clock_in).total_seconds() / 3600, 0)
        total += hours * float(row["hourly_pay_rate"])
    return round(total, 2)
