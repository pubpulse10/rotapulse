"""
UK date formatting ("20 July 2026") — used both as the `uk_date` Jinja
filter (app/__init__.py registers it) and directly from Python for output
that isn't rendered through a template (payroll PDF/CSV exports, weekly
digest and other notification text). One place decides the format, so
every date shown to a user is unambiguous and consistent, rather than
leaking the raw ISO (YYYY-MM-DD) storage format — which reads as
month-first to anyone glancing at it out of context.

format_uk_datetime is the same convention plus a 24-hour time, added for
the timestamp fields (clock_in_at/clock_out_at, notified_at) that used to
bypass this module entirely because format_uk_date isn't datetime-aware —
now mirrored into the sibling apps' own date_format.py, same reasoning.
"""

from datetime import date, datetime


def format_uk_date(value) -> str:
    """Falls back to the original value unchanged if it can't be parsed as
    a date, rather than raising — some date-ish fields across the family
    (e.g. PricePulse's invoice_date) aren't guaranteed to be clean ISO."""
    if not value:
        return value
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return value
    return f"{value.day} {value.strftime('%B %Y')}"


def format_uk_datetime(value) -> str:
    if not value:
        return value
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return f"{value.day} {value.strftime('%B %Y, %H:%M')}"


def format_uk_time(value) -> str:
    """Time only (24-hour, "HH:MM") — for showing an actual clock-in/out
    time alongside a date that's already displayed separately (the rota
    grid's shift panel, the payroll report), where repeating the full date
    via format_uk_datetime would be redundant."""
    if not value:
        return value
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%H:%M")


def variance_label(actual_at, planned_hhmm, threshold_minutes=15) -> str | None:
    """"Early" or "Late" relative to a planned HH:MM, or None if either
    value is missing or the difference is within threshold_minutes (kept
    in sync with staff_portal.VARIANCE_THRESHOLD_MINUTES — same "not worth
    flagging" cutoff as the flag this replaces). Added 2026-08-18 alongside
    ad-hoc/early-start approval: attendance.variance_flag is just a
    boolean (set if either the clock-in or clock-out was >15 min off
    plan), with no sign — so an early arrival was showing under the same
    "Late" badge as a genuinely late one. Direction and magnitude are both
    derived here from the two real timestamps rather than stored, since
    nothing before this needed to distinguish them."""
    if not actual_at or not planned_hhmm:
        return None
    if isinstance(actual_at, str):
        try:
            actual_at = datetime.fromisoformat(actual_at)
        except ValueError:
            return None
    planned = actual_at.replace(hour=int(planned_hhmm[:2]), minute=int(planned_hhmm[3:5]), second=0, microsecond=0)
    diff_minutes = (actual_at - planned).total_seconds() / 60
    if abs(diff_minutes) <= threshold_minutes:
        return None
    return "Early" if diff_minutes < 0 else "Late"
