"""
Pay period boundary calculation (spec §7.1). Pure functions, no DB access,
so they're trivially unit-testable.

Handles the one explicitly-called-out edge case: if
pay_period_month_end_day doesn't exist in a given month (e.g. set to 30,
but the month is February), the period falls back to the last day of that
month.
"""

import calendar
from datetime import date, timedelta


def _month_end_day_for(year: int, month: int, configured_day: int) -> int:
    last_day = calendar.monthrange(year, month)[1]
    return min(configured_day, last_day)


def period_containing(settings_row, on_date: date) -> tuple[date, date]:
    """Returns (start_date, end_date) inclusive, for the pay period that
    on_date falls within, given a venue_settings row."""
    period_type = settings_row["pay_period_type"]

    if period_type == "monthly":
        configured_day = settings_row["pay_period_month_end_day"] or 28
        end_day_this_month = _month_end_day_for(on_date.year, on_date.month, configured_day)
        this_month_end = date(on_date.year, on_date.month, end_day_this_month)
        if on_date <= this_month_end:
            end = this_month_end
            prev_month = on_date.month - 1 or 12
            prev_year = on_date.year if on_date.month > 1 else on_date.year - 1
            prev_end_day = _month_end_day_for(prev_year, prev_month, configured_day)
            start = date(prev_year, prev_month, prev_end_day) + timedelta(days=1)
        else:
            next_month = on_date.month + 1 if on_date.month < 12 else 1
            next_year = on_date.year if on_date.month < 12 else on_date.year + 1
            end = date(next_year, next_month, _month_end_day_for(next_year, next_month, configured_day))
            start = this_month_end + timedelta(days=1)
        return start, end

    # weekly / every_n_weeks — both driven off an anchor date + interval
    interval_weeks = settings_row["pay_period_interval_weeks"] or 1
    anchor_str = settings_row["pay_period_anchor_date"]
    anchor = date.fromisoformat(anchor_str) if anchor_str else on_date
    interval_days = interval_weeks * 7
    days_since_anchor = (on_date - anchor).days
    periods_elapsed = days_since_anchor // interval_days
    start = anchor + timedelta(days=periods_elapsed * interval_days)
    end = start + timedelta(days=interval_days - 1)
    return start, end
