"""
The leave reports, and paid leave reaching the payroll report.

Second half of step 2 of docs/leave-design.md: an all-staff summary for any
date range (backwards or forwards — the owner explicitly wanted to look
ahead), the holiday position for everyone, and the thing the payroll report
never knew about at all, which is that holiday exists.
"""

import csv
import io
from datetime import date

from app import db as db_module
from tests.conftest import create_active_staff, login_as_person, login_as_pub

FIVE_DAYS = '{"mon":true,"tue":true,"wed":true,"thu":true,"fri":true,"sat":false,"sun":false}'


def _staff(app, venue, name, availability=FIVE_DAYS, **detail):
    person_id, membership_id, _e = create_active_staff(app, venue["id"], name=name)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE rota_staff_detail SET availability = ? WHERE venue_membership_id = ?",
                     (availability, membership_id))
        for column, value in detail.items():
            conn.execute(f"UPDATE rota_staff_detail SET {column} = ? WHERE venue_membership_id = ?",
                         (value, membership_id))
        conn.execute("UPDATE venue_settings SET holiday_year_start_date = '01-01' WHERE venue_id = ?",
                     (venue["id"],))
        conn.commit()
    return person_id, membership_id


def _leave(app, venue, person_id, start, end, leave_type="paid"):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            """INSERT INTO leave_request (person_id, venue_id, start_date, end_date, leave_type, status)
               VALUES (?, ?, ?, ?, ?, 'approved')""",
            (person_id, venue["id"], start, end, leave_type),
        )
        conn.commit()


def _report(client, venue, start=None, end=None, fmt=""):
    login_as_pub(client, venue["pub_id"])
    query = f"?start={start}&end={end}" if start else ""
    return client.get(f"/v/{venue['slug']}/leave/{fmt}{query}")


# --------------------------------------------------------------------------- #
# The all-staff report
# --------------------------------------------------------------------------- #

def test_the_report_breaks_leave_down_by_type_for_the_dates_chosen(app, client, venue):
    person_id, _m = _staff(app, venue, "Reported Staff")
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06")                    # 5 paid
    _leave(app, venue, person_id, "2026-03-09", "2026-03-10", leave_type="sick")  # 2 sick
    _leave(app, venue, person_id, "2026-07-06", "2026-07-10")                    # outside the range

    html = _report(client, venue, "2026-03-01", "2026-03-31").data.decode("utf-8")

    row = html[html.index("Reported Staff"):]
    row = row[:row.index("</tr>")]
    assert ">5<" in row and ">2<" in row   # paid and sick
    assert ">7<" in row                    # and the total for the range


def test_the_report_looks_forward_as_well_as_back(app, client, venue):
    """Booked-ahead leave is the point of being able to pick future dates."""
    person_id, _m = _staff(app, venue, "Future Booked")
    _leave(app, venue, person_id, "2027-02-01", "2027-02-05")

    html = _report(client, venue, "2027-01-01", "2027-03-31").data.decode("utf-8")

    row = html[html.index("Future Booked"):]
    assert ">5<" in row[:row.index("</tr>")]


def test_the_position_section_ignores_the_dates_chosen(app, client, venue):
    """A holiday position only means anything against the holiday year it
    belongs to. Re-basing it on whatever range somebody typed would produce a
    "remaining" figure that is true of nothing."""
    person_id, _m = _staff(app, venue, "Year Position")
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06")  # in the year, outside the range below

    html = _report(client, venue, "2026-08-01", "2026-08-31").data.decode("utf-8")

    position = html[html.index("Holiday position this year"):]
    assert "whatever dates are chosen above" in position
    assert ">23<" in position  # 28 allowance less the 5 days taken in March


def test_somebody_without_a_working_pattern_is_named_not_silently_zero(app, client, venue):
    person_id, _m = _staff(app, venue, "No Pattern", availability=None)

    html = _report(client, venue).data.decode("utf-8")

    assert "No Pattern" in html
    assert "Working days not set" in html


def test_staff_who_have_left_still_appear(app, client, venue):
    """Their leave still happened. A report of a past period that quietly
    dropped them would be wrong."""
    person_id, membership_id = _staff(app, venue, "Has Left")
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE venue_membership SET status = 'left' WHERE id = ?", (membership_id,))
        conn.commit()

    assert b"Has Left" in _report(client, venue).data


def test_the_report_is_admin_only(app, client, venue):
    person_id, _m = _staff(app, venue, "Nosey")
    login_as_person(client, person_id)

    resp = client.get(f"/v/{venue['slug']}/leave/")

    assert resp.status_code == 302  # bounced, not shown


def test_the_leave_queue_links_to_the_report(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/rota/leave")
    assert f"/v/{venue['slug']}/leave/".encode() in resp.data


def test_the_csv_export_carries_both_sections(app, client, venue):
    person_id, _m = _staff(app, venue, "Csv Person")
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06")

    resp = _report(client, venue, "2026-01-01", "2026-12-31", fmt="export.csv")
    text = resp.data.decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))

    assert resp.status_code == 200
    assert any("Leave taken" in row[0] for row in rows if row)
    assert any("Holiday position" in row[0] for row in rows if row)
    assert ["Name", "Allowance", "Carried over", "Total", "Taken", "Booked ahead", "Left to book"] in rows
    text.encode("ascii")  # Excel opens a BOM-less UTF-8 CSV as Windows-1252


def test_the_pdf_export_builds(app, client, venue):
    person_id, _m = _staff(app, venue, "Pdf Person")
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06", leave_type="maternity")

    resp = _report(client, venue, "2026-01-01", "2026-12-31", fmt="export.pdf")

    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF")


# --------------------------------------------------------------------------- #
# Paid leave on the payroll report
# --------------------------------------------------------------------------- #

def _payroll(client, venue, fmt=""):
    login_as_pub(client, venue["pub_id"])
    return client.get(f"/v/{venue['slug']}/payroll/{fmt}?start=2026-03-01&end=2026-03-31")


def test_paid_leave_now_shows_on_the_payroll_report(app, client, venue):
    """The report counts clocked hours only, so holiday was silently missing
    from the figures going to whoever runs the wages."""
    person_id, _m = _staff(app, venue, "Holiday Payer", usual_daily_hours=6)
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06")

    html = _payroll(client, venue).data.decode("utf-8")

    assert "Paid leave in this period" in html
    block = html[html.index("Paid leave in this period"):]
    assert "Holiday Payer" in block
    assert ">5<" in block   # days
    assert ">30<" in block  # hours: 5 days at their usual 6-hour day


def test_paid_leave_is_not_added_to_the_gross_pay_total(app, client, venue):
    """Deliberately separate: RotaPulse does not work out holiday pay, and a
    total that silently included an invented figure would be worse than one
    that plainly excludes it."""
    person_id, _m = _staff(app, venue, "Not Totalled", usual_daily_hours=6)
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06")

    html = _payroll(client, venue).data.decode("utf-8")

    assert "doesn&#39;t work out holiday pay" in html or "doesn't work out holiday pay" in html
    # Nobody clocked anything, so there is no pay total at all.
    assert "Total gross pay for all staff" not in html


def test_rolled_up_holiday_pay_is_flagged_so_it_is_not_paid_twice(app, client, venue):
    person_id, _m = _staff(app, venue, "Rolled Up", usual_daily_hours=6, holiday_pay_rolled_up=1)
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06")

    html = _payroll(client, venue).data.decode("utf-8")

    assert "rolled up" in html
    assert "pay again" in html


def test_only_paid_leave_reaches_the_payroll_report(app, client, venue):
    """Sick pay and maternity pay are payroll's own business, on their own
    rules — the rota app must not present them as something to pay."""
    person_id, _m = _staff(app, venue, "Sick Person", usual_daily_hours=6)
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06", leave_type="sick")

    html = _payroll(client, venue).data.decode("utf-8")

    assert "Paid leave in this period" not in html


def test_leave_that_cannot_be_counted_is_named_rather_than_missed(app, client, venue):
    person_id, _m = _staff(app, venue, "Uncounted", availability=None)
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06")

    html = _payroll(client, venue).data.decode("utf-8")

    assert "Uncounted" in html
    assert "Working days not set" in html


def test_the_payroll_csv_carries_the_paid_leave_block(app, client, venue):
    person_id, _m = _staff(app, venue, "Csv Leave", usual_daily_hours=6)
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06")

    resp = _payroll(client, venue, fmt="export.csv")
    text = resp.data.decode("utf-8")

    assert "PAID LEAVE IN THIS PERIOD" in text
    block = text[text.index("PAID LEAVE IN THIS PERIOD"):]
    assert "Csv Leave" in block and "30" in block
    block.encode("ascii")


def test_the_payroll_pdf_builds_with_paid_leave(app, client, venue):
    person_id, _m = _staff(app, venue, "Pdf Leave", usual_daily_hours=6, holiday_pay_rolled_up=1)
    _leave(app, venue, person_id, "2026-03-02", "2026-03-06")

    resp = _payroll(client, venue, fmt="export.pdf")

    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF")
