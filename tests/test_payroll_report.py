from datetime import date, timedelta

from app import db as db_module
from app.pay_periods import period_containing
from tests.conftest import create_active_staff, login_as_pub


def test_payroll_report_computes_gross_pay(app, client, venue):
    person_id, membership_id, _email = create_active_staff(app, venue["id"], name="PayTest")
    today = date.today().isoformat()

    with app.app_context():
        conn = db_module.get_db()
        shift_cur = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id, today),
        )
        shift_id = shift_cur.lastrowid
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at) VALUES (?, ?, ?)",
            (shift_id, f"{today}T09:00:00", f"{today}T17:00:00"),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/payroll/?start={today}&end={today}")
    assert resp.status_code == 200
    # 8 hours * £12.50/hr (create_active_staff's fixed rate) = £100.00
    assert b"100.0" in resp.data or b"100.00" in resp.data


def test_payroll_report_shows_actual_clock_in_and_out_times(app, client, venue):
    """Owner-stated concern: staff are paid for actual time worked, so the
    payroll report needs to show the actual clock times behind each hours
    figure, not just the computed total."""
    person_id, _m, _e = create_active_staff(app, venue["id"], name="ClockTimesTest")
    today = date.today().isoformat()
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id, today),
        ).lastrowid
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at) VALUES (?, ?, ?)",
            (shift_id, f"{today}T09:04:00", f"{today}T17:11:00"),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/payroll/?start={today}&end={today}")
    assert resp.status_code == 200
    assert b"09:04" in resp.data
    assert b"17:11" in resp.data


def test_payroll_csv_export_includes_clock_times(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="CsvClockTest")
    today = date.today().isoformat()
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id, today),
        ).lastrowid
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at) VALUES (?, ?, ?)",
            (shift_id, f"{today}T09:04:00", f"{today}T17:11:00"),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/payroll/export.csv?start={today}&end={today}")
    assert resp.status_code == 200
    assert b"09:04" in resp.data
    assert b"17:11" in resp.data
    assert b"Clocked in" in resp.data


def test_payroll_pdf_export_works_with_data(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="PdfTest")
    today = date.today().isoformat()
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id, today),
        ).lastrowid
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at) VALUES (?, ?, ?)",
            (shift_id, f"{today}T09:00:00", f"{today}T17:00:00"),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/payroll/export.pdf?start={today}&end={today}")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert len(resp.data) > 0


def test_payroll_pdf_export_explains_empty_period_instead_of_blank_table(app, client, venue):
    """Regression coverage for a real report: rostering shifts for a week
    with nobody clocked in/out yet produced a PDF with no explanation —
    looked broken rather than "nothing to report yet". Payroll is
    attendance-based (spec §7.2), not rota-based, so this is legitimately
    empty; the PDF should say so."""
    person_id, _m, _e = create_active_staff(app, venue["id"], name="RosteredNotClockedIn")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, '2026-07-20', '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/payroll/export.pdf?start=2026-07-20&end=2026-07-26")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF")
    # PDF text isn't reliably byte-searchable (ReportLab doesn't lay it out
    # as contiguous plain text) — the CSV export test below covers the
    # same underlying "empty period" message logic in a format that is.


def test_payroll_csv_export_explains_empty_period(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/payroll/export.csv?start=2026-07-20&end=2026-07-26")
    assert resp.status_code == 200
    assert b"haven't clocked in" in resp.data


def test_month_end_day_falls_back_to_last_day_of_february():
    settings_row = {
        "pay_period_type": "monthly",
        "pay_period_month_end_day": 30,
        "pay_period_interval_weeks": None,
        "pay_period_anchor_date": None,
    }
    start, end = period_containing(settings_row, date(2026, 2, 15))
    assert end == date(2026, 2, 28)  # 2026 is not a leap year


def test_weekly_period_boundaries_from_anchor():
    settings_row = {
        "pay_period_type": "weekly",
        "pay_period_interval_weeks": 1,
        "pay_period_anchor_date": "2026-01-05",  # a Monday
        "pay_period_month_end_day": None,
    }
    start, end = period_containing(settings_row, date(2026, 1, 20))
    assert start == date(2026, 1, 19)
    assert end == date(2026, 1, 25)



def test_weekly_period_with_no_anchor_runs_monday_to_sunday_not_from_today():
    """Every venue starts with no anchor date. The current period used to
    start on whatever day the report was opened."""
    settings_row = {
        "pay_period_type": "weekly",
        "pay_period_interval_weeks": 1,
        "pay_period_anchor_date": None,
        "pay_period_month_end_day": None,
    }
    for day in (date(2026, 9, 30), date(2026, 10, 2)):  # a Wednesday, a Friday
        start, end = period_containing(settings_row, day)
        assert (start, end) == (date(2026, 9, 28), date(2026, 10, 4))


def test_fortnightly_period_with_no_anchor_stays_put_from_day_to_day():
    settings_row = {
        "pay_period_type": "every_n_weeks",
        "pay_period_interval_weeks": 2,
        "pay_period_anchor_date": None,
        "pay_period_month_end_day": None,
    }
    first = period_containing(settings_row, date(2026, 9, 28))
    assert first[0].weekday() == 0 and (first[1] - first[0]).days == 13
    assert period_containing(settings_row, first[1]) == first


def _set_pay_settings(app, venue_id, **values):
    with app.app_context():
        conn = db_module.get_db()
        for column, value in values.items():
            conn.execute(f"UPDATE venue_settings SET {column} = ? WHERE venue_id = ?", (value, venue_id))
        conn.commit()


def test_payroll_shows_pay_day_for_a_real_pay_period(app, client, venue):
    _set_pay_settings(app, venue["id"], pay_period_type="weekly", pay_period_interval_weeks=1,
                      pay_period_anchor_date="2026-01-05", pay_day_offset=5)
    login_as_pub(client, venue["pub_id"])

    body = client.get(f"/v/{venue['slug']}/payroll/?start=2026-09-21&end=2026-09-27").get_data(as_text=True)
    assert "Pay day for this period: <strong>2 October 2026</strong>" in body
    assert "No anchor date is set" not in body

    # Not a pay period, so no pay day is claimed for it.
    body = client.get(f"/v/{venue['slug']}/payroll/?start=2026-09-22&end=2026-09-27").get_data(as_text=True)
    assert "Pay day for this period" not in body


def test_payroll_says_when_no_anchor_date_is_set(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    body = client.get(f"/v/{venue['slug']}/payroll/").get_data(as_text=True)
    assert "No anchor date is set for your pay periods" in body
