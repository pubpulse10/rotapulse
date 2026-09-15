"""
Payroll report: telling the admin what the totals leave out.

Real report, 2026-09-15. Preparing for The Cock's first month-end on RotaPulse
(RotaCloud already cancelled), the owner found the payroll totals are built
from completed clock-in/clock-out pairs only. A forgotten clock-out, or a
rostered shift nobody clocked in to, was simply absent — and nothing on the
page said so. The totals looked finished while being short, which for payroll
means someone gets underpaid without anyone noticing.

The clock is pinned (payroll.uk_now) in every test here: whether a shift has
"ended" depends on the time, and this suite has been bitten before by tests
that only passed at certain times of day.
"""

from datetime import datetime

import app.payroll as payroll_module
from app import db as db_module
from app.costs import shift_ends_at
from app.date_format import format_uk_date
from tests.conftest import create_active_staff, login_as_pub

NOW = datetime(2026, 8, 20, 12, 0)
AUGUST = "start=2026-08-01&end=2026-08-31"


def _pin_clock(monkeypatch, now=NOW):
    monkeypatch.setattr(payroll_module, "uk_now", lambda: now)


def _shift(app, venue_id, person_id, shift_date, start, end,
           clock_in=None, clock_out=None, approval=None, origin=None, attendance=None):
    """A shift, plus an attendance row when any clock time or approval is given
    (or attendance=True forces an empty one)."""
    with app.app_context():
        conn = db_module.get_db()
        if origin:
            shift_id = conn.execute(
                "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status, origin) "
                "VALUES (?, ?, ?, ?, ?, 'scheduled', ?)",
                (venue_id, person_id, shift_date, start, end, origin),
            ).lastrowid
        else:
            shift_id = conn.execute(
                "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) "
                "VALUES (?, ?, ?, ?, ?, 'scheduled')",
                (venue_id, person_id, shift_date, start, end),
            ).lastrowid
        if clock_in or clock_out or approval or attendance:
            conn.execute(
                "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at, approval_status) VALUES (?, ?, ?, ?)",
                (shift_id, clock_in, clock_out, approval),
            )
        conn.commit()
        return shift_id


def _report(client, venue, query=AUGUST, fmt=""):
    login_as_pub(client, venue["pub_id"])
    return client.get(f"/v/{venue['slug']}/payroll/{fmt}?{query}")


# --------------------------------------------------------------------------- #
# When a shift has ended
# --------------------------------------------------------------------------- #

def test_shift_ends_at_puts_a_late_shift_finish_on_the_next_day():
    assert shift_ends_at("2026-08-19", "20:00", "02:00") == datetime(2026, 8, 20, 2, 0)
    assert shift_ends_at("2026-08-19", "09:00", "17:00") == datetime(2026, 8, 19, 17, 0)


def test_shift_ends_at_reads_an_ad_hoc_placeholder_as_zero_length_not_a_day():
    assert shift_ends_at("2026-08-19", "18:00", "18:00") == datetime(2026, 8, 19, 18, 0)


# --------------------------------------------------------------------------- #
# Forgotten clock-outs
# --------------------------------------------------------------------------- #

def test_forgotten_clock_out_is_listed_with_a_fix_link_and_left_out_of_totals(app, client, venue, monkeypatch):
    _pin_clock(monkeypatch)
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Lianne Test")
    _shift(app, venue["id"], person_id, "2026-08-10", "17:00", "23:00", clock_in="2026-08-10 17:00:00")
    _shift(app, venue["id"], person_id, "2026-08-11", "09:00", "17:00",
           clock_in="2026-08-11 09:00:00", clock_out="2026-08-11 17:00:00")

    resp = _report(client, venue)

    assert resp.status_code == 200
    assert b"Check these before you run payroll" in resp.data
    assert b"Clocked in, but no clock-out" in resp.data
    assert b"Add clock-out time" in resp.data
    assert f"/rota/cell/{person_id}/2026-08-10".encode() in resp.data
    # Long finished, so this is a missed clock-out, not someone still working.
    assert b"May still be on shift" not in resp.data
    # The complete shift is counted; the incomplete one isn't.
    assert b"<strong>8.0</strong>" in resp.data


def test_a_late_shift_still_running_is_marked_may_still_be_on_shift(app, client, venue, monkeypatch):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Late Shift")
    _shift(app, venue["id"], person_id, "2026-08-20", "20:00", "02:00", clock_in="2026-08-20 20:00:00")

    # 01:00 the next morning: the shift runs to 02:00, so they're still behind the bar.
    _pin_clock(monkeypatch, datetime(2026, 8, 21, 1, 0))
    assert b"May still be on shift" in _report(client, venue).data

    # 04:00: past the 02:00 finish and the hour's grace — now it's a missed clock-out.
    _pin_clock(monkeypatch, datetime(2026, 8, 21, 4, 0))
    resp = _report(client, venue)
    assert b"Clocked in, but no clock-out" in resp.data
    assert b"May still be on shift" not in resp.data


def test_an_unplanned_shift_uses_its_clock_in_not_its_placeholder_end(app, client, venue, monkeypatch):
    """An ad-hoc shift is created with start == end == the clock-in time, so its
    'rostered end' is the moment it began. Going by that would call someone a
    missed clock-out one minute into an unplanned shift."""
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Unplanned")
    _shift(app, venue["id"], person_id, "2026-08-20", "18:00", "18:00",
           clock_in="2026-08-20 18:00:00", origin="ad_hoc")

    _pin_clock(monkeypatch, datetime(2026, 8, 20, 21, 0))
    resp = _report(client, venue)
    assert b"unplanned shift" in resp.data
    assert b"May still be on shift" in resp.data


# --------------------------------------------------------------------------- #
# Rostered, never clocked in
# --------------------------------------------------------------------------- #

def test_rostered_shift_never_clocked_in_is_listed_only_once_it_has_ended(app, client, venue, monkeypatch):
    _pin_clock(monkeypatch, datetime(2026, 8, 20, 1, 0))
    person_id, _m, _e = create_active_staff(app, venue["id"], name="No Show")
    _shift(app, venue["id"], person_id, "2026-08-18", "09:00", "17:00")   # over
    _shift(app, venue["id"], person_id, "2026-08-19", "22:00", "02:00")   # ends 02:00 today: not yet
    _shift(app, venue["id"], person_id, "2026-08-25", "09:00", "17:00")   # next week

    resp = _report(client, venue)

    assert b"On the rota, but never clocked in" in resp.data
    assert format_uk_date("2026-08-18").encode() in resp.data
    assert format_uk_date("2026-08-19").encode() not in resp.data
    assert format_uk_date("2026-08-25").encode() not in resp.data


def test_an_empty_attendance_row_counts_as_never_clocked_in(app, client, venue, monkeypatch):
    """Saving the correction form with both boxes cleared leaves an attendance
    row with no times at all. That is still a shift nobody clocked in to."""
    _pin_clock(monkeypatch)
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Cleared Times")
    _shift(app, venue["id"], person_id, "2026-08-18", "09:00", "17:00", attendance=True)

    assert b"On the rota, but never clocked in" in _report(client, venue).data


def test_rejected_attendance_is_not_reported_as_missing(app, client, venue, monkeypatch):
    """Rejecting attendance is the admin deciding it shouldn't be paid — a
    decision, not a gap, so it must not come back as something to fix."""
    _pin_clock(monkeypatch)
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Rejected")
    _shift(app, venue["id"], person_id, "2026-08-18", "09:00", "17:00",
           clock_in="2026-08-18 09:00:00", approval="rejected")

    assert b"Check these before you run payroll" not in _report(client, venue).data


# --------------------------------------------------------------------------- #
# Clock-out with no clock-in
# --------------------------------------------------------------------------- #

def test_clock_out_without_clock_in_no_longer_breaks_the_report(app, client, venue, monkeypatch):
    """datetime.fromisoformat(None) used to take the whole report down with a
    500 for any date range containing one of these."""
    _pin_clock(monkeypatch)
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Out Only")
    _shift(app, venue["id"], person_id, "2026-08-18", "09:00", "17:00", clock_out="2026-08-18 17:00:00")

    resp = _report(client, venue)
    assert resp.status_code == 200
    assert b"Clock-out time, but no clock-in" in resp.data
    assert b"Add clock-in time" in resp.data

    assert _report(client, venue, fmt="export.csv").status_code == 200
    assert _report(client, venue, fmt="export.pdf").status_code == 200


# --------------------------------------------------------------------------- #
# Pay rate
# --------------------------------------------------------------------------- #

def test_hours_at_no_pay_rate_are_flagged_with_a_link_to_set_one(app, client, venue, monkeypatch):
    """Invited staff start on the schema default of £0 until an admin sets a
    rate, so their hours come out right and their pay comes out as nothing."""
    _pin_clock(monkeypatch)
    person_id, membership_id, _e = create_active_staff(app, venue["id"], name="New Starter")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE rota_staff_detail SET hourly_pay_rate = 0 WHERE venue_membership_id = ?", (membership_id,))
        conn.commit()
    _shift(app, venue["id"], person_id, "2026-08-18", "09:00", "17:00",
           clock_in="2026-08-18 09:00:00", clock_out="2026-08-18 17:00:00")

    resp = _report(client, venue)
    assert b"No pay rate set" in resp.data
    assert b"Set pay rate" in resp.data
    assert f"/staff/{membership_id}/edit".encode() in resp.data


# --------------------------------------------------------------------------- #
# All clear
# --------------------------------------------------------------------------- #

def test_a_complete_period_says_nothing_is_missing(app, client, venue, monkeypatch):
    _pin_clock(monkeypatch)
    person_id, _m, _e = create_active_staff(app, venue["id"], name="All Good")
    _shift(app, venue["id"], person_id, "2026-08-18", "09:00", "17:00",
           clock_in="2026-08-18 09:00:00", clock_out="2026-08-18 17:00:00")

    resp = _report(client, venue)
    assert b"Nothing missing" in resp.data
    assert b"Check these before you run payroll" not in resp.data


# --------------------------------------------------------------------------- #
# Exports carry the warning too
# --------------------------------------------------------------------------- #

def test_csv_export_lists_what_is_not_included_in_plain_ascii(app, client, venue, monkeypatch):
    """The file is what reaches whoever runs the wages, so it must carry the
    warning. ASCII only: Excel opens a BOM-less UTF-8 CSV as Windows-1252 and
    turns a dash or a pound sign into mojibake."""
    _pin_clock(monkeypatch)
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Csv Person")
    _shift(app, venue["id"], person_id, "2026-08-10", "17:00", "23:00", clock_in="2026-08-10 17:00:00")
    _shift(app, venue["id"], person_id, "2026-08-18", "09:00", "17:00")

    resp = _report(client, venue, fmt="export.csv")

    assert resp.status_code == 200
    text = resp.data.decode("utf-8")
    assert "NOT INCLUDED IN THE TOTALS ABOVE" in text
    block = text[text.index("NOT INCLUDED IN THE TOTALS ABOVE"):]
    assert "No clock-out" in block
    assert "Never clocked in" in block
    block.encode("ascii")  # raises if a non-ASCII character crept in


def test_pdf_export_builds_when_shifts_are_not_included(app, client, venue, monkeypatch):
    _pin_clock(monkeypatch)
    person_id, membership_id, _e = create_active_staff(app, venue["id"], name="Pdf & Co")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE rota_staff_detail SET hourly_pay_rate = 0 WHERE venue_membership_id = ?", (membership_id,))
        conn.commit()
    _shift(app, venue["id"], person_id, "2026-08-10", "17:00", "23:00", clock_in="2026-08-10 17:00:00")
    _shift(app, venue["id"], person_id, "2026-08-11", "09:00", "17:00",
           clock_in="2026-08-11 09:00:00", clock_out="2026-08-11 17:00:00")

    resp = _report(client, venue, fmt="export.pdf")

    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF")


# --------------------------------------------------------------------------- #
# The correction form can't create the broken record in the first place
# --------------------------------------------------------------------------- #

def test_saving_a_clock_out_with_the_clock_in_box_empty_is_refused(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Correction")
    shift_id = _shift(app, venue["id"], person_id, "2026-08-10", "17:00", "23:00",
                      clock_in="2026-08-10 17:00:00")
    login_as_pub(client, venue["pub_id"])

    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/attendance",
        data={"clock_in_time": "", "clock_out_time": "23:00"},
        follow_redirects=True,
    )

    assert b"Add the time they clocked in as well" in resp.data
    with app.app_context():
        att = db_module.get_db().execute(
            "SELECT clock_in_at, clock_out_at FROM attendance WHERE shift_id = ?", (shift_id,)
        ).fetchone()
    assert att["clock_in_at"] == "2026-08-10 17:00:00", "the existing clock-in was wiped"
    assert att["clock_out_at"] is None


def test_adding_a_forgotten_late_clock_out_still_lands_on_the_next_day(app, client, venue):
    """The fix this report points people at. It must keep working for the
    commonest case at a pub: a late shift finishing after midnight."""
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Past Midnight")
    shift_id = _shift(app, venue["id"], person_id, "2026-08-10", "20:00", "02:00",
                      clock_in="2026-08-10 20:00:00")
    login_as_pub(client, venue["pub_id"])

    client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/attendance",
        data={"clock_in_time": "20:00", "clock_out_time": "01:30"},
    )

    with app.app_context():
        att = db_module.get_db().execute(
            "SELECT clock_out_at FROM attendance WHERE shift_id = ?", (shift_id,)
        ).fetchone()
    assert att["clock_out_at"] == "2026-08-11 01:30:00"
