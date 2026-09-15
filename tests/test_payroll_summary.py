"""
Payroll summary and grand total.

Owner request, 2026-09-15, during The Cock's month-end dry run: a one-line-
per-person summary (name, hours, gross pay) to send to whoever runs the
wages, and a grand total for all staff at the bottom — how much money the pay
run needs.

Also covers the rounding change that came with it. A person's pay used to be
rounded after every shift, so a month's rounding piled up: 121.40 hours at
£11.44 came out at £1,388.80 against £1,388.82 for hours times rate. On a
summary whose whole point is to be checked, that looks like an error.
"""

import csv
import io
from decimal import ROUND_HALF_UP, Decimal

from app import db as db_module
from tests.conftest import create_active_staff, login_as_pub

AUGUST = "start=2026-08-01&end=2026-08-31"


def _staff(app, venue_id, name, rate):
    person_id, membership_id, _e = create_active_staff(app, venue_id, name=name)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_staff_detail SET hourly_pay_rate = ? WHERE venue_membership_id = ?", (rate, membership_id)
        )
        conn.commit()
    return person_id


def _worked(app, venue_id, person_id, shift_date, clock_in, clock_out, approval=None):
    """A completed shift. clock_in/clock_out are HH:MM:SS on shift_date."""
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) "
            "VALUES (?, ?, ?, ?, ?, 'scheduled')",
            (venue_id, person_id, shift_date, clock_in[:5], clock_out[:5]),
        ).lastrowid
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at, approval_status) VALUES (?, ?, ?, ?)",
            (shift_id, f"{shift_date} {clock_in}", f"{shift_date} {clock_out}", approval),
        )
        conn.commit()


def _two_staff(app, venue):
    alice = _staff(app, venue["id"], "Alice", 12.50)
    bob = _staff(app, venue["id"], "Bob", 11.44)
    _worked(app, venue["id"], alice, "2026-08-10", "09:00:00", "17:00:00")   # 8.00h
    _worked(app, venue["id"], alice, "2026-08-11", "09:00:00", "17:00:00")   # 8.00h -> 16.00h, £200.00
    _worked(app, venue["id"], bob, "2026-08-12", "12:00:00", "17:30:00")     # 5.50h at 11.44 = £62.92


def _get(client, venue, path=""):
    login_as_pub(client, venue["pub_id"])
    return client.get(f"/v/{venue['slug']}/payroll/{path}?{AUGUST}")


def _csv_rows(resp):
    return list(csv.reader(io.StringIO(resp.data.decode("utf-8"))))


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

def test_summary_has_one_line_per_person_with_hours_rate_and_gross_pay(app, client, venue):
    _two_staff(app, venue)
    html = _get(client, venue).data.decode("utf-8")

    summary = html[html.index("<h2>Summary</h2>"):html.index("Shift-by-shift detail")]
    assert "Alice" in summary and "16.00" in summary and "£12.50" in summary and "£200.00" in summary
    assert "Bob" in summary and "5.50" in summary and "£11.44" in summary and "£62.92" in summary
    assert "All staff" in summary and "21.50" in summary and "£262.92" in summary


def test_the_summary_comes_before_the_detail_and_the_grand_total_closes_the_page(app, client, venue):
    _two_staff(app, venue)
    html = _get(client, venue).data.decode("utf-8")

    summary = html.index("<h2>Summary</h2>")
    detail = html.index("Shift-by-shift detail")
    grand_total = html.index("Total gross pay for all staff")
    assert summary < detail < grand_total
    # Nothing but the totals box follows the last person's detail table.
    assert html.rindex("</table>") < grand_total
    assert "£262.92" in html[grand_total:]
    assert "21.50 hours · 2 people" in html[grand_total:]


def test_grand_total_says_it_is_gross_and_excludes_employer_costs(app, client, venue):
    """It is an indication of the money the pay run needs, so it must not be
    mistaken for the whole cost of employing people."""
    _two_staff(app, venue)
    html = _get(client, venue).data.decode("utf-8")
    assert "Employer&#39;s National Insurance and pension contributions are not included" in html \
        or "Employer's National Insurance and pension contributions are not included" in html


def test_grand_total_mentions_hours_still_awaiting_approval(app, client, venue):
    carl = _staff(app, venue["id"], "Carl", 12.00)
    _worked(app, venue["id"], carl, "2026-08-10", "08:00:00", "16:00:00", approval="pending")
    html = _get(client, venue).data.decode("utf-8")
    assert "includes 8.00 hours still awaiting approval" in html


def test_an_empty_period_shows_no_summary_and_no_grand_total(app, client, venue):
    html = _get(client, venue).data.decode("utf-8")
    assert "<h2>Summary</h2>" not in html
    assert "Total gross pay for all staff" not in html
    assert "No completed shifts in this period yet." in html


# --------------------------------------------------------------------------- #
# The arithmetic
# --------------------------------------------------------------------------- #

def test_gross_pay_is_hours_times_rate_rounded_once_not_shift_by_shift(app, client, venue):
    """20 shifts of 6.07 hours (6h 4m 12s) at £11.44: 121.40 x 11.44 =
    1388.816 -> £1,388.82. Rounding after every shift gave £1,388.80."""
    dana = _staff(app, venue["id"], "Dana", 11.44)
    for day in range(1, 21):
        _worked(app, venue["id"], dana, f"2026-08-{day:02d}", "09:00:00", "15:04:12")

    html = _get(client, venue).data.decode("utf-8")

    assert "121.40" in html
    assert "£1,388.82" in html
    assert "£1,388.80" not in html
    assert "£69.44" in html  # each shift: 6.07 x 11.44 = 69.4408


def test_gross_pay_rounds_an_exact_half_penny_up_not_down(app, client, venue):
    """3h30m at £12.21 is £42.735 — exactly half a penny. Python's round() works
    on the binary float, which holds 42.735 as 42.73499999..., so the old code
    paid £42.73. Rounding half up in Decimal pays £42.74.

    Not a one-off: checking every shift length from 3 to 10 hours against
    every rate from £10.00 to £13.99, over 3,600 combinations came out a
    penny short under the old rounding."""
    ed = _staff(app, venue["id"], "Ed", 12.21)
    _worked(app, venue["id"], ed, "2026-08-10", "18:00:00", "21:30:00")

    html = _get(client, venue).data.decode("utf-8")

    assert "£42.74" in html
    assert "£42.73" not in html


def test_the_gross_pay_column_adds_up_to_the_grand_total_to_the_penny(app, client, venue):
    """Awkward rates and odd minutes on purpose: the check a payroll person
    makes first is whether the column sums to the total."""
    for name, rate, shifts in [
        ("Fay", 11.44, [("09:00:00", "15:04:12"), ("17:00:00", "23:20:00"), ("10:30:00", "14:47:24")]),
        ("Gus", 12.21, [("08:00:00", "15:19:48"), ("12:00:00", "20:01:12")]),
        ("Hal", 10.42, [("18:00:00", "22:40:12"), ("11:15:00", "16:59:24"), ("09:00:00", "13:24:36")]),
    ]:
        person = _staff(app, venue["id"], name, rate)
        for i, (cin, cout) in enumerate(shifts):
            _worked(app, venue["id"], person, f"2026-08-{10 + i:02d}", cin, cout)

    rows = _csv_rows(_get(client, venue, "export.csv"))
    header = rows.index(["Name", "Hours", "Hourly rate", "Gross pay"])
    people, all_staff = [], None
    for row in rows[header + 1:]:
        if row and row[0] == "All staff":
            all_staff = row
            break
        people.append(row)

    assert len(people) == 3
    assert sum(Decimal(r[3]) for r in people) == Decimal(all_staff[3])
    assert sum(Decimal(r[1]) for r in people) == Decimal(all_staff[1])
    # And every line checks by hand: hours x rate = gross pay, to the penny.
    for name, hours, rate, pay in people:
        assert (Decimal(hours) * Decimal(rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == Decimal(pay), name


# --------------------------------------------------------------------------- #
# The exports
# --------------------------------------------------------------------------- #

def test_csv_leads_with_the_summary_and_ends_the_detail_with_an_all_staff_total(app, client, venue):
    _two_staff(app, venue)
    resp = _get(client, venue, "export.csv")
    rows = _csv_rows(resp)

    assert rows[0][0] == "Payroll summary: 1 August 2026 to 31 August 2026"
    assert rows[1] == ["Name", "Hours", "Hourly rate", "Gross pay"]
    assert ["Alice", "16.00", "12.50", "200.00"] in rows
    assert ["Bob", "5.50", "11.44", "62.92"] in rows
    assert ["All staff", "21.50", "", "262.92"] in rows
    assert ["All staff", "TOTAL", "", "", "21.50", "262.92", ""] in rows
    # Plain numbers only, so a spreadsheet can add them up.
    assert "£" not in resp.data.decode("utf-8")
    assert "1,388" not in resp.data.decode("utf-8")


def test_pdf_export_builds_with_the_summary_and_grand_total(app, client, venue):
    _two_staff(app, venue)
    resp = _get(client, venue, "export.pdf")
    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF")
