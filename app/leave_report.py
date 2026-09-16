"""
The leave reports (step 2 of docs/leave-design.md).

Two questions, one screen, because they are asked together:

  1. What leave did everyone take between these dates? Any range, backwards
     or forwards — the owner explicitly wanted to be able to look ahead as
     well as back.
  2. Where does everyone stand this holiday year? Allowance, taken, booked
     ahead, what is left.

The second is deliberately NOT tied to the dates picked for the first. A
holiday position only means anything against the holiday year it belongs to,
and quietly re-basing it on whatever range somebody typed would produce a
"remaining" figure that is true of nothing.

Admin only. A staff member sees their own position on their own Leave page.
"""

import csv
import io
from datetime import date

import flask

from app.date_format import format_uk_date
from app.db import get_db
from app.leave import (LEAVE_TYPES, holiday_year_bounds, leave_days_in_window, position)
from app.rota_auth import register_identity, require_permission
from app.uk_time import uk_today
from app.venue_scope import register_venue_gate, register_venue_scope

leave_report_bp = flask.Blueprint("leave_report", __name__, url_prefix="/v/<slug>/leave")
register_venue_scope(leave_report_bp)
register_venue_gate(leave_report_bp)
register_identity(leave_report_bp)

TYPE_KEYS = [key for key, _label, _r, _a in LEAVE_TYPES]
TYPE_LABELS = [label for _key, label, _r, _a in LEAVE_TYPES]


def _staff_rows(db, venue_id):
    """Everyone with a staff record at this venue, including people who have
    left: their leave still happened, and a report of a past period that
    quietly dropped them would be wrong."""
    return db.execute(
        """SELECT venue_membership.id AS membership_id, person.id AS person_id, person.name
           FROM venue_membership
           JOIN person ON person.id = venue_membership.person_id
           JOIN rota_staff_detail ON rota_staff_detail.venue_membership_id = venue_membership.id
           WHERE venue_membership.venue_id = ?
           ORDER BY person.name""",
        (venue_id,),
    ).fetchall()


def _gather(db, venue_id, start_date: str, end_date: str, today=None):
    """Both halves of the report, for the page and for both exports."""
    today = today or uk_today()
    settings = db.execute("SELECT * FROM venue_settings WHERE venue_id = ?", (venue_id,)).fetchone()
    year_start, year_end = holiday_year_bounds(
        settings["holiday_year_start_date"] if settings else None, today
    )
    window_start = date.fromisoformat(start_date)
    window_end = date.fromisoformat(end_date)

    period, positions = [], []
    for member in _staff_rows(db, venue_id):
        detail = db.execute(
            "SELECT * FROM rota_staff_detail WHERE venue_membership_id = ?", (member["membership_id"],)
        ).fetchone()
        availability = detail["availability"] if detail else None

        by_type = {
            key: leave_days_in_window(db, member["person_id"], availability, window_start, window_end, (key,))
            for key in TYPE_KEYS
        }
        counted = [days for days in by_type.values() if days is not None]
        period.append({
            "name": member["name"],
            "membership_id": member["membership_id"],
            "by_type": by_type,
            "total": round(sum(counted), 2) if counted else None,
            # None everywhere means we can't count this person's days at all.
            "countable": by_type["paid"] is not None,
        })

        pos = position(db, member["person_id"], member["membership_id"], detail, settings, today=today)
        pos["name"] = member["name"]
        pos["membership_id"] = member["membership_id"]
        positions.append(pos)

    return {
        "period": period,
        "positions": positions,
        "year_start": year_start,
        "year_end": year_end,
        "start_date": start_date,
        "end_date": end_date,
    }


def _requested_range(db, venue_id, today=None):
    """Defaults to the holiday year, which is the range somebody opening this
    screen almost always wants."""
    today = today or uk_today()
    start = flask.request.args.get("start")
    end = flask.request.args.get("end")
    if start and end and start <= end:
        return start, end
    settings = db.execute("SELECT * FROM venue_settings WHERE venue_id = ?", (venue_id,)).fetchone()
    year_start, year_end = holiday_year_bounds(
        settings["holiday_year_start_date"] if settings else None, today
    )
    return year_start.isoformat(), year_end.isoformat()


@leave_report_bp.route("/")
@require_permission("app_admin", "rota_admin")
def report():
    db = get_db()
    venue_id = flask.g.venue["id"]
    start_date, end_date = _requested_range(db, venue_id)
    data = _gather(db, venue_id, start_date, end_date)
    return flask.render_template(
        "leave/report.html", type_keys=TYPE_KEYS, type_labels=TYPE_LABELS, **data
    )


def _export_rows(data):
    """Flattened for CSV and PDF: the period table, then the positions."""
    period = [["Name"] + TYPE_LABELS + ["Total"]]
    for row in data["period"]:
        if not row["countable"]:
            period.append([row["name"]] + ["-"] * len(TYPE_KEYS) + ["working days not set"])
            continue
        period.append([row["name"]] + [f"{row['by_type'][key]:g}" for key in TYPE_KEYS] + [f"{row['total']:g}"])

    positions = [["Name", "Allowance", "Carried over", "Total", "Taken", "Booked ahead", "Left to book"]]
    for pos in data["positions"]:
        if not pos["countable"] or pos["total"] is None:
            positions.append([pos["name"], "-", "-", "-", "-", "-", "not set"])
            continue
        positions.append([
            pos["name"], f"{pos['allowance']['days']:g}", f"{pos['carried_over']:g}", f"{pos['total']:g}",
            f"{pos['taken']:g}", f"{pos['booked']:g}", f"{pos['remaining']:g}",
        ])
    return period, positions


@leave_report_bp.route("/export.csv")
@require_permission("app_admin", "rota_admin")
def export_csv():
    db = get_db()
    venue_id = flask.g.venue["id"]
    start_date, end_date = _requested_range(db, venue_id)
    data = _gather(db, venue_id, start_date, end_date)
    period, positions = _export_rows(data)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    # Plain ASCII and plain numbers: Excel opens a BOM-less UTF-8 CSV as
    # Windows-1252 and garbles anything else (same reasoning as the payroll
    # export).
    writer.writerow([f"Leave taken: {format_uk_date(start_date)} to {format_uk_date(end_date)}"])
    writer.writerows(period)
    writer.writerow([])
    writer.writerow([f"Holiday position: year ending {format_uk_date(data['year_end'].isoformat())}"])
    writer.writerows(positions)

    resp = flask.Response(buffer.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f"attachment; filename=leave_{start_date}_to_{end_date}.csv"
    return resp


@leave_report_bp.route("/export.pdf")
@require_permission("app_admin", "rota_admin")
def export_pdf():
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from xml.sax.saxutils import escape

    db = get_db()
    venue = flask.g.venue
    start_date, end_date = _requested_range(db, venue["id"])
    data = _gather(db, venue["id"], start_date, end_date)
    period, positions = _export_rows(data)

    style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#06223b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ])

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph(f"Leave: {escape(venue['name'])}", styles["Title"]),
        Paragraph(f"Taken between {format_uk_date(start_date)} and {format_uk_date(end_date)}, in days",
                  styles["Heading2"]),
    ]
    table = Table(period, colWidths=[130] + [58] * len(TYPE_KEYS) + [50])
    table.setStyle(style)
    elements.append(table)

    elements.append(Spacer(1, 10))
    elements.append(Paragraph(
        f"Holiday position for the year ending {format_uk_date(data['year_end'].isoformat())}",
        styles["Heading2"],
    ))
    position_table = Table(positions, colWidths=[130, 62, 68, 50, 50, 68, 62])
    position_table.setStyle(style)
    elements.append(position_table)
    elements.append(Spacer(1, 8))
    elements.append(Paragraph(
        "Only paid leave comes off a holiday balance. Sick, unpaid, maternity and days in lieu are "
        "recorded and shown above, but do not reduce anyone's entitlement.",
        styles["Normal"],
    ))
    doc.build(elements)

    resp = flask.Response(buffer.getvalue(), mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f"attachment; filename=leave_{start_date}_to_{end_date}.pdf"
    return resp
