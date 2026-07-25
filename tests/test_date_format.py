from datetime import date, datetime

from app.date_format import format_uk_date, format_uk_datetime


def test_format_uk_date_from_iso_string():
    assert format_uk_date("2026-07-20") == "20 July 2026"


def test_format_uk_date_from_date_object():
    assert format_uk_date(date(2026, 7, 6)) == "6 July 2026"  # no zero-padding, matches the rest of the app


def test_format_uk_date_passes_through_falsy_values():
    assert format_uk_date(None) is None
    assert format_uk_date("") == ""


def test_format_uk_date_tolerates_a_full_datetime_string():
    """Some callers hand this a full "YYYY-MM-DD HH:MM:SS" value (e.g. a
    SQLite datetime('now') column) when only the date matters — slicing
    to the first 10 chars means this never raises, unlike a bare
    date.fromisoformat() call would on a pre-3.11-style full timestamp."""
    assert format_uk_date("2026-07-20 14:32:01") == "20 July 2026"


def test_uk_date_jinja_filter_is_registered(app):
    assert "uk_date" in app.jinja_env.filters
    assert app.jinja_env.filters["uk_date"]("2026-01-01") == "1 January 2026"


def test_format_uk_datetime_from_iso_string():
    assert format_uk_datetime("2026-07-20 14:32:01") == "20 July 2026, 14:32"


def test_format_uk_datetime_from_datetime_object():
    assert format_uk_datetime(datetime(2026, 7, 6, 9, 5)) == "6 July 2026, 09:05"


def test_format_uk_datetime_passes_through_falsy_values():
    assert format_uk_datetime(None) is None
    assert format_uk_datetime("") == ""


def test_uk_datetime_jinja_filter_is_registered(app):
    assert "uk_datetime" in app.jinja_env.filters
    assert app.jinja_env.filters["uk_datetime"]("2026-01-01 08:00:00") == "1 January 2026, 08:00"
