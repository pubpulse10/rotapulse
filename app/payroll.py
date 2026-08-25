"""
Payroll report (spec §7.2) — not a stored entity, a query over ATTENDANCE
for a selected date range. RotaPulse is explicitly not a payroll system: no
tax, pension, or deduction handling, gross pay only, for manual handover to
whoever actually runs payroll.
"""

import csv
import io

import flask

from app.date_format import format_uk_date, format_uk_time
from app.db import get_db
from app.pay_periods import period_containing
from app.rota_auth import register_identity, require_permission
from app.uk_time import uk_today
from app.venue_scope import register_venue_gate, register_venue_scope

payroll_bp = flask.Blueprint("payroll", __name__, url_prefix="/v/<slug>/payroll")
register_venue_scope(payroll_bp)
register_venue_gate(payroll_bp)
register_identity(payroll_bp)


def _report_rows(db, venue_id, start_date, end_date):
    # Rejected attendance (see app/rota_grid.py::reject_attendance) is
    # excluded from pay outright — an admin rejecting it is asserting it
    # shouldn't be paid as claimed. Pending rows are NOT excluded: per a
    # direct 2026-08-18 decision, hours count as soon as they're clocked so
    # a slow admin can't accidentally short someone honest — pending_hours
    # below just surfaces which figures are still provisional.
    rows = db.execute(
        """SELECT person.id AS person_id, person.name, shift.shift_date,
                  attendance.clock_in_at, attendance.clock_out_at, attendance.approval_status,
                  rota_staff_detail.hourly_pay_rate
           FROM attendance
           JOIN shift ON shift.id = attendance.shift_id
           JOIN person ON person.id = shift.person_id
           JOIN venue_membership ON venue_membership.person_id = person.id AND venue_membership.venue_id = shift.venue_id
           JOIN rota_staff_detail ON rota_staff_detail.venue_membership_id = venue_membership.id
           WHERE shift.venue_id = ? AND shift.shift_date BETWEEN ? AND ?
           AND attendance.clock_out_at IS NOT NULL
           AND (attendance.approval_status IS NULL OR attendance.approval_status != 'rejected')
           ORDER BY person.name, shift.shift_date""",
        (venue_id, start_date, end_date),
    ).fetchall()

    from datetime import datetime as dt

    by_person = {}
    pending_hours = 0.0
    for row in rows:
        clock_in = dt.fromisoformat(row["clock_in_at"])
        clock_out = dt.fromisoformat(row["clock_out_at"])
        hours = round(max((clock_out - clock_in).total_seconds() / 3600, 0), 2)
        rate = float(row["hourly_pay_rate"])
        entry = by_person.setdefault(
            row["person_id"], {"name": row["name"], "days": [], "total_hours": 0.0, "total_pay": 0.0}
        )
        entry["days"].append({
            "date": row["shift_date"], "hours": hours, "pay": round(hours * rate, 2),
            "clock_in_at": row["clock_in_at"], "clock_out_at": row["clock_out_at"],
            "approval_status": row["approval_status"],
        })
        entry["total_hours"] = round(entry["total_hours"] + hours, 2)
        entry["total_pay"] = round(entry["total_pay"] + hours * rate, 2)
        if row["approval_status"] == "pending":
            pending_hours = round(pending_hours + hours, 2)
    return by_person, pending_hours


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
    return flask.render_template(
        "payroll/report.html", by_person=by_person, pending_hours=pending_hours,
        start_date=start_date, end_date=end_date,
    )


@payroll_bp.route("/export.csv")
@require_permission("app_admin", "rota_admin")
def export_csv():
    db = get_db()
    venue = flask.g.venue
    start_date = flask.request.args["start"]
    end_date = flask.request.args["end"]
    by_person, pending_hours = _report_rows(db, venue["id"], start_date, end_date)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
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
                day["hours"], day["pay"], day["approval_status"] or "",
            ])
        writer.writerow([entry["name"], "TOTAL", "", "", entry["total_hours"], entry["total_pay"], ""])
    if pending_hours:
        writer.writerow([f"{pending_hours} of the hours above are still awaiting admin approval."])

    resp = flask.Response(buffer.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f"attachment; filename=payroll_{start_date}_to_{end_date}.csv"
    return resp


@payroll_bp.route("/export.pdf")
@require_permission("app_admin", "rota_admin")
def export_pdf():
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet

    db = get_db()
    venue = flask.g.venue
    start_date = flask.request.args["start"]
    end_date = flask.request.args["end"]
    by_person, pending_hours = _report_rows(db, venue["id"], start_date, end_date)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = [Paragraph(
        f"Payroll report: {venue['name']} — {format_uk_date(start_date)} to {format_uk_date(end_date)}",
        styles["Title"],
    )]

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
        data = [["Name", "Date", "Clocked in", "Clocked out", "Hours", "Pay", "Approval"]]
        for entry in by_person.values():
            for day in entry["days"]:
                data.append([
                    entry["name"], format_uk_date(day["date"]),
                    format_uk_time(day["clock_in_at"]) or "", format_uk_time(day["clock_out_at"]) or "",
                    day["hours"], f"£{day['pay']:.2f}", day["approval_status"] or "",
                ])
            data.append([entry["name"], "TOTAL", "", "", entry["total_hours"], f"£{entry['total_pay']:.2f}", ""])

        table = Table(data, colWidths=[95, 75, 60, 60, 50, 65, 65])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#06223b")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        elements.append(table)
        if pending_hours:
            elements.append(Paragraph(
                f"{pending_hours} of the hours above are still awaiting admin approval — figures may change.",
                styles["Normal"],
            ))
    doc.build(elements)

    resp = flask.Response(buffer.getvalue(), mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f"attachment; filename=payroll_{start_date}_to_{end_date}.pdf"
    return resp
