from datetime import date

from app.date_format import format_uk_date


def test_format_uk_date_from_iso_string():
    assert format_uk_date("2026-07-20") == "20 July 2026"


def test_format_uk_date_from_date_object():
    assert format_uk_date(date(2026, 7, 6)) == "6 July 2026"  # no zero-padding, matches the rest of the app


def test_format_uk_date_passes_through_falsy_values():
    assert format_uk_date(None) is None
    assert format_uk_date("") == ""


def test_uk_date_jinja_filter_is_registered(app):
    assert "uk_date" in app.jinja_env.filters
    assert app.jinja_env.filters["uk_date"]("2026-01-01") == "1 January 2026"
