"""
Payroll report (spec §7.2) — not a stored entity, a query over ATTENDANCE
for a selected date range. RotaPulse is explicitly not a payroll system: no
tax, pension, or deduction handling, gross pay only, for manual handover to
whoever actually runs payroll.
"""

import csv
import io
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from xml.sax.saxutils import escape

import flask

from app.costs import shift_ends_at
from app.date_format import format_uk_date, format_uk_time
from app.db import get_db
from app.pay_periods import period_containing
from app.rota_auth import register_identity, require_permission
from app.uk_time import uk_now, uk_today
from app.venue_scope import register_venue_gate, register_venue_scope

payroll_bp = flask.Blueprint("payroll", __name__, url_prefix="/v/<slug>/payroll")
register_venue_scope(payroll_bp)
register_venue_gate(payroll_bp)
register_identity(payroll_bp)


def _money(hours, rate):
    """Pay for a number of hours at a rate, rounded ONCE to the penny, half up.

    2026-09-15: a person's total used to be rounded after every shift
    (total = round(total + hours * rate, 2)), so a month's rounding piled up.
    121.40 hours at £11.44 came out at £1,388.80 against £1,388.82 for hours
    times rate. Harmless-looking until it is on a summary sent to whoever runs
    the wages, where "hours x rate = gross pay" is the first thing checked.
    Decimal because a float like 75.875 can sit just below .5 and round down.
    """
    pay = Decimal(str(hours)) * Decimal(str(rate))
    return float(pay.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@payroll_bp.app_template_filter("gbp")
def format_gbp(value):
    """£1,234.56 — thousands separator, because a grand total for a whole pay
    run is routinely four figures."""
    return f"£{value:,.2f}"


def _report_rows(db, venue_id, start_date, end_date):
    # Rejected attendance (see app/rota_grid.py::reject_attendance) is
    # excluded from pay outright — an admin rejecting it is asserting it
    # shouldn't be paid as claimed. Pending rows are NOT excluded: per a
    # direct 2026-08-18 decision, hours count as soon as they're clocked so
    # a slow admin can't accidentally short someone honest — pending_hours
    # below just surfaces which figures are still provisional.
    #
    # A clock-out with no clock-in has no length, and datetime.fromisoformat(None)
    # below used to take the whole report down with a 500, for every date range
    # that included it. It is skipped here and listed by _needs_attention instead.
    rows = db.execute(
        """SELECT person.id AS person_id, venue_membership.id AS membership_id,
                  person.name, shift.shift_date,
                  attendance.clock_in_at, attendance.clock_out_at, attendance.approval_status,
                  rota_staff_detail.hourly_pay_rate
           FROM attendance
           JOIN shift ON shift.id = attendance.shift_id
           JOIN person ON person.id = shift.person_id
           JOIN venue_membership ON venue_membership.person_id = person.id AND venue_membership.venue_id = shift.venue_id
           JOIN rota_staff_detail ON rota_staff_detail.venue_membership_id = venue_membership.id
           WHERE shift.venue_id = ? AND shift.shift_date BETWEEN ? AND ?
           AND attendance.clock_in_at IS NOT NULL AND attendance.clock_out_at IS NOT NULL
           AND (attendance.approval_status IS NULL OR attendance.approval_status != 'rejected')
           ORDER BY person.name, shift.shift_date""",
        (venue_id, start_date, end_date),
    ).fetchall()

    by_person = {}
    pending_hours = 0.0
    for row in rows:
        clock_in = datetime.fromisoformat(row["clock_in_at"])
        clock_out = datetime.fromisoformat(row["clock_out_at"])
        hours = round(max((clock_out - clock_in).total_seconds() / 3600, 0), 2)
        rate = float(row["hourly_pay_rate"])
        entry = by_person.setdefault(
            row["person_id"], {
                "name": row["name"], "membership_id": row["membership_id"], "rate": rate,
                "days": [], "total_hours": 0.0, "total_pay": 0.0,
            },
        )
        entry["days"].append({
            "date": row["shift_date"], "hours": hours, "pay": _money(hours, rate),
            "clock_in_at": row["clock_in_at"], "clock_out_at": row["clock_out_at"],
            "approval_status": row["approval_status"],
        })
        entry["total_hours"] = round(entry["total_hours"] + hours, 2)
        if row["approval_status"] == "pending":
            pending_hours = round(pending_hours + hours, 2)
    # Gross pay is the person's total hours times their rate, worked out once
    # here rather than accumulated shift by shift — see _money().
    for entry in by_person.values():
        entry["total_pay"] = _money(entry["total_hours"], entry["rate"])
    return by_person, pending_hours


def _summary(by_person):
    """The all-staff totals for the one-line-per-person summary and the grand
    total (owner request, 2026-09-15: the summary is what goes to whoever runs
    the wages, and the grand total is how much money this pay run needs).

    The grand total is the sum of the per-person figures exactly as displayed,
    in Decimal, so the Gross pay column always adds up to it to the penny.
    """
    total_pay = sum((Decimal(str(e["total_pay"])) for e in by_person.values()), Decimal("0"))
    total_hours = sum((Decimal(str(e["total_hours"])) for e in by_person.values()), Decimal("0"))
    return {"total_hours": float(total_hours), "total_pay": float(total_pay), "people": len(by_person)}


# How long after a rostered shift's end we still say "may still be on shift"
# rather than presenting it as a missed clock-out: people stay behind the bar
# past their rostered finish. It only softens the wording. The shift is listed
# either way, because its hours are missing from the totals either way.
STILL_ON_SHIFT_GRACE = timedelta(hours=1)

# An unplanned (ad-hoc) shift has no rostered end to go by: it is created with
# start and end both set to the clock-in time (staff_portal.start_ad_hoc_shift).
# Treat one as possibly still running for this long after its clock-in.
AD_HOC_ASSUMED_LENGTH = timedelta(hours=12)


def _needs_attention(db, venue_id, start_date, end_date, by_person, now):
    """Everything in the date range that the totals can't include, each with
    what's wrong and a link to where it gets fixed.

    Real report, 2026-09-15: preparing for The Cock's first month-end on
    RotaPulse (RotaCloud already cancelled), the owner found that the totals
    are built from completed clock-in/clock-out pairs only. A forgotten
    clock-out, or a rostered shift nobody clocked in to, was simply absent,
    and nothing on the page said so. The totals looked finished while being
    short. This is what says so.

    A shift with no clock-in is listed only once it has ENDED (shift_ends_at,
    which handles a late shift running past midnight), so a report run
    mid-month doesn't flag shifts that haven't happened yet. Rejected
    attendance is left out on purpose: rejecting it is the admin deciding it
    shouldn't be paid, which is a decision, not a gap.
    """
    rows = db.execute(
        """SELECT shift.id AS shift_id, shift.shift_date, shift.start_time, shift.end_time,
                  shift.origin, person.id AS person_id, person.name,
                  attendance.clock_in_at, attendance.clock_out_at
           FROM shift
           JOIN person ON person.id = shift.person_id
           LEFT JOIN attendance ON attendance.shift_id = shift.id
           WHERE shift.venue_id = ? AND shift.shift_date BETWEEN ? AND ?
           AND shift.status = 'scheduled'
           AND (attendance.approval_status IS NULL OR attendance.approval_status != 'rejected')
           AND (attendance.clock_in_at IS NULL OR attendance.clock_out_at IS NULL)
           ORDER BY shift.shift_date, shift.start_time, person.name""",
        (venue_id, start_date, end_date),
    ).fetchall()

    attention = {"no_clock_out": [], "clock_out_only": [], "no_clock_in": [], "no_pay_rate": []}
    for row in rows:
        ad_hoc = row["origin"] == "ad_hoc"
        item = {
            "name": row["name"],
            "shift_date": row["shift_date"],
            "shift_label": "Unplanned" if ad_hoc else f"{row['start_time']}-{row['end_time']}",
            "clock_in_at": row["clock_in_at"],
            "clock_out_at": row["clock_out_at"],
            "fix_url": flask.url_for("rota_grid.cell", person_id=row["person_id"], on_date=row["shift_date"]),
        }
        ends_at = shift_ends_at(row["shift_date"], row["start_time"], row["end_time"])
        if row["clock_in_at"] and not row["clock_out_at"]:
            if ad_hoc:
                likely_finish = datetime.fromisoformat(row["clock_in_at"]) + AD_HOC_ASSUMED_LENGTH
            else:
                likely_finish = ends_at + STILL_ON_SHIFT_GRACE
            item["may_still_be_on_shift"] = now < likely_finish
            attention["no_clock_out"].append(item)
        elif row["clock_out_at"] and not row["clock_in_at"]:
            attention["clock_out_only"].append(item)
        elif ends_at <= now:
            attention["no_clock_in"].append(item)

    # Hours ARE counted for these people, but at £0.00. Anyone who joined
    # through an invite starts on the schema default of 0 until an admin sets
    # a rate (onboarding.py inserts rota_staff_detail without one), so this is
    # the likeliest way for a new starter's first month to come out unpaid.
    for entry in by_person.values():
        if entry["rate"] <= 0 and entry["total_hours"] > 0:
            attention["no_pay_rate"].append({
                "name": entry["name"],
                "hours": entry["total_hours"],
                "fix_url": flask.url_for("admin_config.edit_staff", membership_id=entry["membership_id"]),
            })
    return attention


def _attention_export_rows(attention):
    """The not-included shifts flattened for the CSV and PDF exports. The file
    is what reaches whoever runs the wages, so it has to carry the warning as
    well as the page. Plain hyphens rather than dashes: Excel opens a UTF-8
    CSV without a BOM as Windows-1252 and garbles anything outside ASCII."""
    rows = []
    for r in attention["no_clock_out"]:
        missing = "No clock-out (still on shift?)" if r["may_still_be_on_shift"] else "No clock-out"
        rows.append([r["name"], format_uk_date(r["shift_date"]), r["shift_label"],
                     format_uk_time(r["clock_in_at"]) or "", "", missing])
    for r in attention["clock_out_only"]:
        rows.append([r["name"], format_uk_date(r["shift_date"]), r["shift_label"],
                     "", format_uk_time(r["clock_out_at"]) or "", "No clock-in"])
    for r in attention["no_clock_in"]:
        rows.append([r["name"], format_uk_date(r["shift_date"]), r["shift_label"], "", "", "Never clocked in"])
    return rows


@payroll_bp.route("/")
@require_permission("app_admin", "rota_admin")
def report():
    db = get_db()
    venue = flask.g.venue
    settings = db.execute("SELECT * FROM venue_settings WHERE venue_id = ?", (venue["id"],)).fetchone()

    start_param = flask.request.args.get("start")
    end_param = flask.request.args.get("end")
    if start_param and end_param:
        start_date, end_date = start_param, end_param
    else:
        period_start, period_end = period_containing(settings, uk_today())
        start_date, end_date = period_start.isoformat(), period_end.isoformat()

    by_person, pending_hours = _report_rows(db, venue["id"], start_date, end_date)
    attention = _needs_attention(db, venue["id"], start_date, end_date, by_person, uk_now())
    return flask.render_template(
        "payroll/report.html", by_person=by_person, pending_hours=pending_hours,
        attention=attention, summary=_summary(by_person), start_date=start_date, end_date=end_date,
    )


@payroll_bp.route("/export.csv")
@require_permission("app_admin", "rota_admin")
def export_csv():
    db = get_db()
    venue = flask.g.venue
    start_date = flask.request.args["start"]
    end_date = flask.request.args["end"]
    by_person, pending_hours = _report_rows(db, venue["id"], start_date, end_date)
    summary = _summary(by_person)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    # Money is written as plain numbers (no £, no thousands separator) so a
    # spreadsheet treats it as numbers it can add up, not as text.
    if by_person:
        # The summary leads: one line per person is what whoever runs the
        # wages works from. The shift-by-shift detail follows as the evidence.
        writer.writerow([f"Payroll summary: {format_uk_date(start_date)} to {format_uk_date(end_date)}"])
        writer.writerow(["Name", "Hours", "Hourly rate", "Gross pay"])
        for entry in by_person.values():
            writer.writerow([entry["name"], f"{entry['total_hours']:.2f}",
                             f"{entry['rate']:.2f}", f"{entry['total_pay']:.2f}"])
        writer.writerow(["All staff", f"{summary['total_hours']:.2f}", "", f"{summary['total_pay']:.2f}"])
        writer.writerow([])
        writer.writerow(["Shift-by-shift detail"])
    writer.writerow(["Name", "Date", "Clocked in", "Clocked out", "Hours", "Pay", "Approval"])
    if not by_person:
        # Same reasoning as export_pdf's empty-state message — payroll is
        # attendance-based, so a fully-rostered week can still be empty
        # here until staff actually clock in and out.
        writer.writerow(["No completed shifts in this period — staff haven't clocked in/out yet for these dates."])
    for entry in by_person.values():
        for day in entry["days"]:
            writer.writerow([
                entry["name"], format_uk_date(day["date"]),
                format_uk_time(day["clock_in_at"]), format_uk_time(day["clock_out_at"]),
                f"{day['hours']:.2f}", f"{day['pay']:.2f}", day["approval_status"] or "",
            ])
        writer.writerow([entry["name"], "TOTAL", "", "", f"{entry['total_hours']:.2f}", f"{entry['total_pay']:.2f}", ""])
    if by_person:
        writer.writerow(["All staff", "TOTAL", "", "", f"{summary['total_hours']:.2f}", f"{summary['total_pay']:.2f}", ""])
    if pending_hours:
        writer.writerow([f"{pending_hours} of the hours above are still awaiting admin approval."])

    attention = _needs_attention(db, venue["id"], start_date, end_date, by_person, uk_now())
    missing = _attention_export_rows(attention)
    if missing or attention["no_pay_rate"]:
        writer.writerow([])
        writer.writerow(["NOT INCLUDED IN THE TOTALS ABOVE - fix these in RotaPulse, then export again."])
        if missing:
            writer.writerow(["Name", "Date", "Shift", "Clocked in", "Clocked out", "What's missing"])
            writer.writerows(missing)
        for r in attention["no_pay_rate"]:
            writer.writerow([r["name"], "", "", "", "",
                             f"No pay rate set, so their {r['hours']} hours above are paid at 0.00"])

    resp = flask.Response(buffer.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f"attachment; filename=payroll_{start_date}_to_{end_date}.csv"
    return resp


@payroll_bp.route("/export.pdf")
@require_permission("app_admin", "rota_admin")
def export_pdf():
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet

    db = get_db()
    venue = flask.g.venue
    start_date = flask.request.args["start"]
    end_date = flask.request.args["end"]
    by_person, pending_hours = _report_rows(db, venue["id"], start_date, end_date)
    summary = _summary(by_person)

    navy_header = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#06223b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
    ]

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = [Paragraph(
        f"Payroll report: {escape(venue['name'])} — {format_uk_date(start_date)} to {format_uk_date(end_date)}",
        styles["Title"],
    )]

    attention = _needs_attention(db, venue["id"], start_date, end_date, by_person, uk_now())
    missing = _attention_export_rows(attention)
    if missing:
        noun = "shift is" if len(missing) == 1 else "shifts are"
        elements.append(Paragraph(
            f"<b>{len(missing)} {noun} not included in these totals</b> because a clock time is missing. "
            "They are listed at the end of this report.",
            styles["Normal"],
        ))

    if not by_person:
        # Payroll is built from ATTENDANCE (actual clocked hours), not the
        # rota (SHIFT) — spec §7.2. A week can be fully rostered and still
        # have nothing here if no one has clocked in/out yet, which reads
        # as "broken" in a silently-empty PDF rather than "nothing to
        # report yet". Say so explicitly instead of just an empty table.
        elements.append(Paragraph(
            "No completed shifts in this period — payroll is calculated from actual clock-in/clock-out "
            "times, not the rota, so it stays empty until staff have clocked in and out for these dates.",
            styles["Normal"],
        ))
    else:
        # Summary first, on page one: it is the part whoever runs the wages
        # actually needs. The detail below it is the evidence.
        elements.append(Paragraph("Summary", styles["Heading2"]))
        summary_rows = [["Name", "Hours", "Hourly rate", "Gross pay"]]
        for entry in by_person.values():
            summary_rows.append([entry["name"], f"{entry['total_hours']:.2f}",
                                 format_gbp(entry["rate"]), format_gbp(entry["total_pay"])])
        summary_rows.append(["All staff", f"{summary['total_hours']:.2f}", "", format_gbp(summary["total_pay"])])
        summary_table = Table(summary_rows, colWidths=[190, 80, 90, 100])
        summary_table.setStyle(TableStyle(navy_header + [
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eef2f6")),
        ]))
        elements.append(summary_table)

        elements.append(Paragraph("Shift-by-shift detail", styles["Heading2"]))
        data = [["Name", "Date", "Clocked in", "Clocked out", "Hours", "Pay", "Approval"]]
        for entry in by_person.values():
            for day in entry["days"]:
                data.append([
                    entry["name"], format_uk_date(day["date"]),
                    format_uk_time(day["clock_in_at"]) or "", format_uk_time(day["clock_out_at"]) or "",
                    f"{day['hours']:.2f}", format_gbp(day["pay"]), day["approval_status"] or "",
                ])
            data.append([entry["name"], "TOTAL", "", "", f"{entry['total_hours']:.2f}", format_gbp(entry["total_pay"]), ""])

        table = Table(data, colWidths=[95, 75, 60, 60, 50, 65, 65])
        table.setStyle(TableStyle(navy_header))
        elements.append(table)

        # The grand total closes the report: how much money this pay run needs.
        elements.append(Spacer(1, 10))
        elements.append(Paragraph(
            f"<b>Total gross pay for all staff: {format_gbp(summary['total_pay'])}</b> "
            f"({summary['total_hours']:.2f} hours)",
            styles["Heading3"],
        ))
        elements.append(Paragraph(
            "Gross pay, before tax and National Insurance are deducted. Employer's National Insurance "
            "and pension contributions are not included.",
            styles["Normal"],
        ))
        if pending_hours:
            elements.append(Paragraph(
                f"{pending_hours} of the hours above are still awaiting admin approval — figures may change.",
                styles["Normal"],
            ))

    if missing or attention["no_pay_rate"]:
        elements.append(Paragraph("Not included in these totals", styles["Heading2"]))
        if missing:
            not_included = Table(
                [["Name", "Date", "Shift", "Clocked in", "Clocked out", "What's missing"]] + missing,
                colWidths=[80, 78, 62, 46, 46, 140],
            )
            not_included.setStyle(TableStyle(navy_header + [("FONTSIZE", (0, 0), (-1, -1), 9)]))
            elements.append(not_included)
        for r in attention["no_pay_rate"]:
            elements.append(Paragraph(
                f"{escape(r['name'])} has no pay rate set, so their {r['hours']} hours above are paid at £0.00.",
                styles["Normal"],
            ))
    doc.build(elements)

    resp = flask.Response(buffer.getvalue(), mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f"attachment; filename=payroll_{start_date}_to_{end_date}.pdf"
    return resp
