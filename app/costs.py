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
        start_h, start_m = map(int, row["start_time"].split(":"))
        end_h, end_m = map(int, row["end_time"].split(":"))
        hours = (end_h * 60 + end_m - start_h * 60 - start_m) / 60
        total += max(hours, 0) * float(row["hourly_pay_rate"])
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
