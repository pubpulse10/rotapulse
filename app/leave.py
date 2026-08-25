"""
Holiday days-taken counter (spec §8): a "day" is defined relative to the
person — only dates within an approved leave request that fall on a day
they'd normally work (per their own availability pattern) count. A day
they never work anyway doesn't add to the total.

Reset boundary: a landlord-defined year-start date per venue (MM-DD, not a
fixed calendar year) — mirrors the pay-period settings' own pattern.
"""

import json
from datetime import date, timedelta

from app.uk_time import uk_today

WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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


def days_taken_count(db, person_id: int, availability_json: str, year_start_mmdd: str, today=None) -> int:
    today = today or uk_today()
    year_start = _current_holiday_year_start(year_start_mmdd, today)
    availability = json.loads(availability_json) if availability_json else {}

    rows = db.execute(
        """SELECT start_date, end_date FROM leave_request
           WHERE person_id = ? AND status = 'approved' AND end_date >= ?""",
        (person_id, year_start.isoformat()),
    ).fetchall()

    count = 0
    for row in rows:
        start = max(date.fromisoformat(row["start_date"]), year_start)
        end = min(date.fromisoformat(row["end_date"]), today)
        d = start
        while d <= end:
            if availability.get(WEEKDAY_KEYS[d.weekday()], True):
                count += 1
            d += timedelta(days=1)
    return count
