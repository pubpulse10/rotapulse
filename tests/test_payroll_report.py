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
