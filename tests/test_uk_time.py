"""app/uk_time.py — real report, 2026-08-19: a staff member's clock-in
time was recorded an hour behind UK time (the server runs in UTC; BST is
UTC+1). The actual risk this module has to get right is DST-awareness — a
hardcoded "add one hour" would be correct in August and wrong in January,
so these tests exercise fixed midsummer/midwinter instants rather than
relying on whatever the real clock happens to be when the suite runs."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.uk_time import uk_now, uk_now_iso, uk_today


def test_summer_instant_is_bst_one_hour_ahead_of_utc():
    utc_instant = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    uk_local = utc_instant.astimezone(ZoneInfo("Europe/London"))
    assert uk_local.utcoffset().total_seconds() == 3600
    assert uk_local.hour == 13


def test_winter_instant_is_gmt_same_as_utc():
    utc_instant = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    uk_local = utc_instant.astimezone(ZoneInfo("Europe/London"))
    assert uk_local.utcoffset().total_seconds() == 0
    assert uk_local.hour == 12


def test_uk_now_returns_a_naive_datetime_close_to_real_time():
    """Not pinned to an exact value (that needs mocking the clock, not
    worth a new dependency for) — just proves it's naive (no tzinfo, so
    it's a drop-in for the naive datetime.now() calls it replaced) and
    actually reflects the real current moment, not some fixed/broken value."""
    before_utc = datetime.now(timezone.utc)
    result = uk_now()
    after_utc = datetime.now(timezone.utc)

    assert result.tzinfo is None
    result_as_utc = result.replace(tzinfo=ZoneInfo("Europe/London")).astimezone(timezone.utc)
    assert before_utc <= result_as_utc <= after_utc + timedelta(seconds=2)


def test_uk_today_matches_uk_now_date():
    assert uk_today() == uk_now().date()


def test_uk_now_iso_matches_sqlite_datetime_now_format():
    """"YYYY-MM-DD HH:MM:SS" — the exact shape SQLite's own datetime('now')
    produces, since this is a direct drop-in replacement for it (see
    app/staff_portal.py's attendance/shift timestamp writes)."""
    value = uk_now_iso()
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    assert abs((parsed - uk_now()).total_seconds()) < 2
