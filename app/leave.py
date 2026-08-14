"""
Holiday days-taken counter (spec §8): a "day" is defined relative to the
person — only dates within an approved leave request that fall on a day
they'd normally work (per their own availability pattern) count. A day
they never work anyway doesn't add to the total.

Reset boundary: a landlord-defined year-start date per venue (MM-DD, not a
fixed calendar year) — mirrors the pay-period settings' own pattern.
"""

import json
import re
from datetime import date, timedelta

WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_SEPARATED = re.compile(r"\d{1,2}[-/. ]\d{1,2}")


def normalize_holiday_year_start(raw: str) -> str | None:
    """Best-effort parse of a day/month entry into the canonical MM-DD this
    module stores and expects — landlords shouldn't have to remember to
    type a literal dash. Accepts '01-01', '01/01', '01.01', '01 01', '1-1',
    and plain 4-digit MM-DD like '0101'. A bare 3-digit run (e.g. '101') is
    deliberately NOT guessed at — it's genuinely ambiguous between
    "1st, 01" and "10th, 1" depending which side is truncated, and getting
    that silently wrong is worse than asking for another attempt. Returns
    None if it can't be confidently parsed as a real month/day.
    """
    if not raw:
        return None
    raw = raw.strip()
    if _SEPARATED.fullmatch(raw):
        month_s, day_s = re.split(r"[-/. ]", raw)
    elif re.fullmatch(r"\d{4}", raw):
        month_s, day_s = raw[:2], raw[2:]
    else:
        return None
    try:
        month, day = int(month_s), int(day_s)
        date(2024, month, day)  # 2024 is a leap year, so 29 Feb validates too
    except ValueError:
        return None
    return f"{month:02d}-{day:02d}"


def _current_holiday_year_start(year_start_mmdd: str, today: date) -> date:
    """Falls back to 1 Jan for anything that isn't a clean MM-DD, rather
    than raising — the settings field is free text with no format
    enforcement before this, and a malformed saved value (e.g. "0101"
    instead of "01-01", a real one found in production) must not take
    down every staff member's leave page at that venue. Saving now
    validates the format (see admin_config.py's settings route), but this
    stays defensive for whatever's already stored from before that."""
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
    today = today or date.today()
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
