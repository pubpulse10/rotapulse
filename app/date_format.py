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
