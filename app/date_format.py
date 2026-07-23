"""
UK date formatting ("20 July 2026") — used both as the `uk_date` Jinja
filter (app/__init__.py registers it) and directly from Python for output
that isn't rendered through a template (payroll PDF/CSV exports, weekly
digest and other notification text). One place decides the format, so
every date shown to a user is unambiguous and consistent, rather than
leaking the raw ISO (YYYY-MM-DD) storage format — which reads as
month-first to anyone glancing at it out of context.
"""

from datetime import date


def format_uk_date(value) -> str:
    if not value:
        return value
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return f"{value.day} {value.strftime('%B %Y')}"
