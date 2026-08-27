"""
The mobile-first, staff-facing side of RotaPulse (spec §5.3): a simple
day-by-day scrollable list of the logged-in person's own shifts, clock-
in/out, leave requests, and open-shift claiming. Available to all three
permission tiers (an app_admin/rota_admin who is also a working staff
member uses this same view for their own shifts).
"""

import json
from datetime import date, timedelta

import flask

from app.db import get_db
from app.geo_distance import distance_metres
from app.leave import days_taken_count
from app.media import save_attendance_photo
from app.notification_settings import notify_admins
from app.rota_auth import register_identity, require_permission
from app.rota_grid import WEEKDAY_KEYS, _billable_staff, _is_on_approved_leave, _monday_of, _week_dates
from app.uk_time import uk_now, uk_now_iso, uk_today
from app.venue_scope import register_venue_gate, register_venue_scope

staff_bp = flask.Blueprint("staff_portal", __name__, url_prefix="/v/<slug>/staff")
register_venue_scope(staff_bp)
register_venue_gate(staff_bp)
register_identity(staff_bp)


def _own_membership(db, venue_id, person_id):
    return db.execute(
        "SELECT * FROM venue_membership WHERE person_id = ? AND venue_id = ?", (person_id, venue_id)
    ).fetchone()


@staff_bp.route("/")
@require_permission("staff", "rota_admin", "app_admin")
def home():
    db = get_db()
    person = flask.g.person
    venue = flask.g.venue
    today = uk_today()
    horizon = today + timedelta(days=21)
    shifts = db.execute(
        """SELECT shift.*, attendance.clock_in_at, attendance.clock_out_at, attendance.approval_status
           FROM shift LEFT JOIN attendance ON attendance.shift_id = shift.id
           WHERE shift.venue_id = ? AND shift.person_id = ? AND shift.status = 'scheduled'
           AND shift.shift_date BETWEEN ? AND ?
           ORDER BY shift.shift_date, shift.start_time""",
        (venue["id"], person["id"], today.isoformat(), horizon.isoformat()),
    ).fetchall()
    # Governs whether "Start an unplanned shift" is offered (see
    # start_ad_hoc_shift) — already clocked into something today means
    # that's the shift to use instead.
    has_open_shift_today = any(s["shift_date"] == today.isoformat() and not s["clock_out_at"] for s in shifts)
    return flask.render_template(
        "staff/home.html", shifts=shifts, today=today.isoformat(), has_open_shift_today=has_open_shift_today
    )


@staff_bp.route("/rota")
@require_permission("staff", "rota_admin", "app_admin")
def full_rota():
    """Real request, 2026-08-19: staff could only ever see their OWN
    shifts, with no way to check who else is on with them or who's
    covering a shift they're handing over. A read-only mirror of the admin
    week grid (app/rota_grid.py::week) — same grid-building helpers, same
    cell states — minus everything only an admin needs: costs, weather,
    drag-and-drop, notify/copy/clear. Deliberately not wired into
    rota_dragdrop.js at all (no data-shift-id/tabindex on the chips) since
    nothing on this page is meant to be draggable or clickable."""
    db = get_db()
    venue = flask.g.venue
    week_param = flask.request.args.get("week")
    week_start = _monday_of(date.fromisoformat(week_param)) if week_param else _monday_of(uk_today())
    dates = _week_dates(week_start)
    date_strs = [d.isoformat() for d in dates]

    staff = _billable_staff(db, venue["id"])
    shift_rows = db.execute(
        """SELECT * FROM shift WHERE venue_id = ? AND shift_date BETWEEN ? AND ? AND status = 'scheduled'""",
        (venue["id"], date_strs[0], date_strs[-1]),
    ).fetchall()
    shifts_by_person_date = {}
    for s in shift_rows:
        shifts_by_person_date.setdefault((s["person_id"], s["shift_date"]), []).append(s)

    overrides = db.execute(
        "SELECT * FROM day_off_override WHERE venue_id = ? AND override_date BETWEEN ? AND ?",
        (venue["id"], date_strs[0], date_strs[-1]),
    ).fetchall()
    override_set = {(o["person_id"], o["override_date"]) for o in overrides}

    open_shifts = db.execute(
        """SELECT shift.*, venue_role.name AS role_name FROM shift
           LEFT JOIN venue_role ON venue_role.id = shift.venue_role_id
           WHERE shift.venue_id = ? AND shift.status = 'open'
           AND shift.shift_date BETWEEN ? AND ?
           ORDER BY shift.shift_date, shift.start_time""",
        (venue["id"], date_strs[0], date_strs[-1]),
    ).fetchall()
    open_shifts_by_date = {}
    for s in open_shifts:
        open_shifts_by_date.setdefault(s["shift_date"], []).append(s)

    grid = []
    for member in staff:
        availability = json.loads(member["availability"]) if member["availability"] else {}
        row_cells = []
        for d, d_str in zip(dates, date_strs):
            if _is_on_approved_leave(db, member["person_id"], d_str):
                cell = {"state": "leave"}
            elif shifts_by_person_date.get((member["person_id"], d_str)):
                cell = {"state": "shift", "shifts": shifts_by_person_date[(member["person_id"], d_str)]}
            elif (member["person_id"], d_str) in override_set:
                cell = {"state": "day_off"}
            elif not availability.get(WEEKDAY_KEYS[d.weekday()], True):
                cell = {"state": "day_off"}
            else:
                cell = {"state": "empty"}
            row_cells.append(cell)
        grid.append({"member": member, "cells": row_cells})

    return flask.render_template(
        "staff/full_rota.html",
        venue=venue,
        week_start=week_start,
        dates=dates,
        grid=grid,
        open_shifts_by_date=open_shifts_by_date,
        prev_week=(week_start - timedelta(days=7)).isoformat(),
        next_week=(week_start + timedelta(days=7)).isoformat(),
    )


@staff_bp.route("/shift/<int:shift_id>")
@require_permission("staff", "rota_admin", "app_admin")
def shift_detail(shift_id):
    db = get_db()
    shift_row = db.execute(
        "SELECT * FROM shift WHERE id = ? AND venue_id = ? AND person_id = ?",
        (shift_id, flask.g.venue["id"], flask.g.person["id"]),
    ).fetchone()
    if shift_row is None:
        flask.abort(404)
    attendance = db.execute("SELECT * FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
    # Swap-target dropdown (spec §5.5): every other active staff member at
    # this venue, so nobody has to know/ask for a colleague's person ID.
    colleagues = db.execute(
        """SELECT person.id, person.name FROM venue_membership
           JOIN person ON person.id = venue_membership.person_id
           WHERE venue_membership.venue_id = ? AND venue_membership.status = 'active'
           AND venue_membership.person_id != ?
           ORDER BY person.name""",
        (flask.g.venue["id"], flask.g.person["id"]),
    ).fetchall()
    return flask.render_template(
        "staff/shift_detail.html",
        shift=shift_row,
        attendance=attendance,
        today=uk_today().isoformat(),
        colleagues=colleagues,
    )


@staff_bp.route("/shift/<int:shift_id>/clock-in", methods=["POST"])
@require_permission("staff", "rota_admin", "app_admin")
def clock_in(shift_id):
    db = get_db()
    venue = flask.g.venue
    shift_row = db.execute(
        "SELECT * FROM shift WHERE id = ? AND venue_id = ? AND person_id = ?",
        (shift_id, venue["id"], flask.g.person["id"]),
    ).fetchone()
    if shift_row is None:
        flask.abort(404)
    # Real report, 2026-08-18: nothing stopped clocking in for a shift days
    # in the future straight from "My shifts" (which lists up to 3 weeks
    # ahead) -- restricted to the shift's own calendar day, matching how
    # attendance is meant to represent when someone actually was in.
    if shift_row["shift_date"] != uk_today().isoformat():
        flask.flash("You can only clock in on the day of the shift itself.", "error")
        return flask.redirect(flask.url_for("staff_portal.shift_detail", shift_id=shift_id))

    form = flask.request.form
    lat = form.get("lat", type=float)
    lng = form.get("lng", type=float)
    location_confirmed = None
    if lat is not None and lng is not None and venue["latitude"] is not None and venue["longitude"] is not None:
        location_confirmed = 1 if distance_metres(lat, lng, venue["latitude"], venue["longitude"]) <= _radius() else 0
    # A staff member who declines the location permission still clocks in
    # successfully (spec §6.1) — location_confirmed stays NULL, not a block.

    photo_url = None
    photo_file = flask.request.files.get("photo")
    if photo_file and photo_file.filename:
        photo_url = save_attendance_photo(photo_file)

    variance = _variance_minutes(shift_row["start_time"])
    variance_flag = 1 if abs(variance) > VARIANCE_THRESHOLD_MINUTES else 0
    # Real request, 2026-08-18: a genuinely rostered shift needs no sign-off
    # to start a little early (someone dragged in because it's already
    # busy) — but starting well ahead of plan is effectively extra,
    # unplanned hours and should go through the same approval as a fully
    # ad-hoc shift (see start_ad_hoc_shift below). Only early starts count;
    # a late start is covered by the existing Late badge, not approval.
    approval_status = "pending" if variance < -EARLY_CLOCK_IN_GRACE_MINUTES else None

    db.execute(
        """INSERT INTO attendance
           (shift_id, clock_in_at, clock_in_lat, clock_in_lng, clock_in_location_confirmed, photo_url,
            variance_flag, approval_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(shift_id) DO UPDATE SET clock_in_at = excluded.clock_in_at,
           clock_in_lat = excluded.clock_in_lat, clock_in_lng = excluded.clock_in_lng,
           clock_in_location_confirmed = excluded.clock_in_location_confirmed,
           photo_url = COALESCE(excluded.photo_url, attendance.photo_url),
           variance_flag = excluded.variance_flag, approval_status = excluded.approval_status""",
        (shift_id, uk_now_iso(), lat, lng, location_confirmed, photo_url, variance_flag, approval_status),
    )
    db.commit()
    if approval_status == "pending":
        notify_admins(
            db, venue, "ad_hoc_shift",
            f"Early clock-in needs approval — {venue['name']}",
            f"{flask.g.person['name']} clocked in {-variance:.0f} minutes early for their "
            f"{shift_row['shift_date']} {shift_row['start_time']} shift at {venue['name']} — needs approval.",
        )
        flask.flash("Clocked in — you're more than 30 minutes early, so this needs admin approval.")
    else:
        flask.flash("Clocked in." if location_confirmed != 0 else "Clocked in — location not confirmed.")
    return flask.redirect(flask.url_for("staff_portal.shift_detail", shift_id=shift_id))


EARLY_CLOCK_IN_GRACE_MINUTES = 30


@staff_bp.route("/shift/ad-hoc/clock-in", methods=["POST"])
@require_permission("staff", "rota_admin", "app_admin")
def start_ad_hoc_shift():
    """Real request, 2026-08-18: staff previously had no way to clock in at
    all without a pre-existing shift — the only options were "admin adds a
    shift first" or "don't work". This lets any registered staff member
    start working on the spot; it always needs admin approval afterwards
    (unlike an early start against a REAL rostered shift, see clock_in()
    above), since there was no plan at all to measure against. Deliberately
    reuses the shift/attendance tables rather than a separate model — a
    shift row is required anyway (attendance is keyed to one), and doing it
    this way means the rota grid, payroll report and cell-detail panel all
    just work with zero extra plumbing; shift.origin is purely a display
    marker to tell admins it wasn't rostered."""
    db = get_db()
    venue = flask.g.venue
    person = flask.g.person
    today = uk_today().isoformat()

    # If they already have an open (not-yet-clocked-out) shift today, that's
    # the one to clock into — sends them there instead of creating a second,
    # overlapping ad-hoc record.
    open_shift = db.execute(
        """SELECT shift.id FROM shift LEFT JOIN attendance ON attendance.shift_id = shift.id
           WHERE shift.venue_id = ? AND shift.person_id = ? AND shift.shift_date = ?
           AND attendance.clock_out_at IS NULL
           ORDER BY shift.start_time LIMIT 1""",
        (venue["id"], person["id"], today),
    ).fetchone()
    if open_shift:
        flask.flash("You already have a shift today — clock in from that instead.", "error")
        return flask.redirect(flask.url_for("staff_portal.shift_detail", shift_id=open_shift["id"]))

    form = flask.request.form
    lat = form.get("lat", type=float)
    lng = form.get("lng", type=float)
    location_confirmed = None
    if lat is not None and lng is not None and venue["latitude"] is not None and venue["longitude"] is not None:
        location_confirmed = 1 if distance_metres(lat, lng, venue["latitude"], venue["longitude"]) <= _radius() else 0

    photo_url = None
    photo_file = flask.request.files.get("photo")
    if photo_file and photo_file.filename:
        photo_url = save_attendance_photo(photo_file)

    # start_time/end_time both placeholder to "now" — real report,
    # 2026-08-19: this used to be SQLite's own strftime('%H:%M','now'),
    # which (like datetime('now') below) is always UTC, not UK local time.
    # Both now come from the same uk_now() call so they can never disagree
    # with each other (see the matching note in clock_out() for an ad-hoc
    # shift's end_time) or, more importantly, with the UK wall clock.
    now = uk_now()
    now_hhmm = now.strftime("%H:%M")
    cur = db.execute(
        """INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status, origin)
           VALUES (?, ?, ?, ?, ?, 'scheduled', 'ad_hoc')""",
        (venue["id"], person["id"], today, now_hhmm, now_hhmm),
    )
    shift_id = cur.lastrowid
    db.execute(
        """INSERT INTO attendance
           (shift_id, clock_in_at, clock_in_lat, clock_in_lng, clock_in_location_confirmed, photo_url,
            variance_flag, approval_status)
           VALUES (?, ?, ?, ?, ?, ?, 0, 'pending')""",
        (shift_id, now.strftime("%Y-%m-%d %H:%M:%S"), lat, lng, location_confirmed, photo_url),
    )
    db.commit()
    notify_admins(
        db, venue, "ad_hoc_shift",
        f"Unplanned shift started — {venue['name']}",
        f"{person['name']} started an unplanned shift at {venue['name']} — needs approval.",
    )
    flask.flash("Shift started — this wasn't rostered, so it'll need admin approval.")
    return flask.redirect(flask.url_for("staff_portal.shift_detail", shift_id=shift_id))


@staff_bp.route("/shift/<int:shift_id>/clock-out", methods=["POST"])
@require_permission("staff", "rota_admin", "app_admin")
def clock_out(shift_id):
    db = get_db()
    venue = flask.g.venue
    shift_row = db.execute(
        "SELECT * FROM shift WHERE id = ? AND venue_id = ? AND person_id = ?",
        (shift_id, venue["id"], flask.g.person["id"]),
    ).fetchone()
    if shift_row is None:
        flask.abort(404)

    form = flask.request.form
    lat = form.get("lat", type=float)
    lng = form.get("lng", type=float)
    location_confirmed = None
    if lat is not None and lng is not None and venue["latitude"] is not None and venue["longitude"] is not None:
        location_confirmed = 1 if distance_metres(lat, lng, venue["latitude"], venue["longitude"]) <= _radius() else 0

    end_variance = 1 if abs(_variance_minutes(shift_row["end_time"])) > VARIANCE_THRESHOLD_MINUTES else 0
    now = uk_now()
    db.execute(
        """UPDATE attendance SET clock_out_at = ?, clock_out_lat = ?, clock_out_lng = ?,
           clock_out_location_confirmed = ?, variance_flag = MAX(variance_flag, ?) WHERE shift_id = ?""",
        (now.strftime("%Y-%m-%d %H:%M:%S"), lat, lng, location_confirmed, end_variance, shift_id),
    )
    if shift_row["origin"] == "ad_hoc":
        # An ad-hoc shift's end_time was only ever a same-instant placeholder
        # set at clock-in (see start_ad_hoc_shift) — there was no real plan
        # to preserve, so update it to the actual clock-out time rather than
        # leaving the grid showing a zero-length shift forever. Uses the SAME
        # `now` just written to attendance.clock_out_at above, not a second
        # separately-computed one, so the two can never disagree.
        db.execute("UPDATE shift SET end_time = ? WHERE id = ?", (now.strftime("%H:%M"), shift_id))
    db.commit()
    flask.flash("Clocked out." if location_confirmed != 0 else "Clocked out — location not confirmed.")
    return flask.redirect(flask.url_for("staff_portal.shift_detail", shift_id=shift_id))


VARIANCE_THRESHOLD_MINUTES = 15


def _radius():
    from app import config

    return config.CLOCK_IN_RADIUS_METRES


def _variance_minutes(planned_hhmm: str) -> float:
    """Minutes between now and the planned HH:MM, on today's date — a
    simple, adequate proxy for "materially different from plan" (spec §6.2)
    without needing to track a running clock client-side. Signed: positive
    means now is AFTER the planned time (late), negative means BEFORE it
    (early) — callers that only care about "how far off, either direction"
    should abs() the result themselves (see VARIANCE_THRESHOLD_MINUTES
    checks); clock_in()'s approval check needs the sign, so this can't just
    return the absolute value like the old _minutes_late did."""
    planned_h, planned_m = map(int, planned_hhmm.split(":"))
    now = uk_now()
    planned = now.replace(hour=planned_h, minute=planned_m, second=0, microsecond=0)
    return (now - planned).total_seconds() / 60


# ---------- Leave (own) ----------


@staff_bp.route("/leave", methods=["GET", "POST"])
@require_permission("staff", "rota_admin", "app_admin")
def leave():
    db = get_db()
    person = flask.g.person
    venue = flask.g.venue

    if flask.request.method == "POST":
        form = flask.request.form
        db.execute(
            "INSERT INTO leave_request (person_id, venue_id, start_date, end_date) VALUES (?, ?, ?, ?)",
            (person["id"], venue["id"], form["start_date"], form["end_date"]),
        )
        db.commit()
        notify_admins(
            db, venue, "leave_request",
            f"Leave request — {venue['name']}",
            f"{person['name']} has requested leave from {form['start_date']} to {form['end_date']} at {venue['name']}.",
        )
        flask.flash("Leave request submitted — awaiting approval.")
        return flask.redirect(flask.url_for("staff_portal.leave"))

    requests_rows = db.execute(
        "SELECT * FROM leave_request WHERE person_id = ? AND venue_id = ? ORDER BY start_date DESC",
        (person["id"], venue["id"]),
    ).fetchall()

    detail = db.execute(
        """SELECT rota_staff_detail.availability, venue_settings.holiday_year_start_date
           FROM venue_membership
           JOIN rota_staff_detail ON rota_staff_detail.venue_membership_id = venue_membership.id
           JOIN venue_settings ON venue_settings.venue_id = venue_membership.venue_id
           WHERE venue_membership.person_id = ? AND venue_membership.venue_id = ?""",
        (person["id"], venue["id"]),
    ).fetchone()
    days_taken = 0
    if detail:
        days_taken = days_taken_count(db, person["id"], detail["availability"], detail["holiday_year_start_date"])

    return flask.render_template("staff/leave.html", requests=requests_rows, days_taken=days_taken)


# ---------- Open shifts (spec §5.4) ----------


@staff_bp.route("/open-shifts")
@require_permission("staff", "rota_admin", "app_admin")
def open_shifts():
    db = get_db()
    rows = db.execute(
        """SELECT shift.*, venue_role.name AS role_name FROM shift
           LEFT JOIN venue_role ON venue_role.id = shift.venue_role_id
           WHERE shift.venue_id = ? AND shift.status = 'open' AND shift.shift_date >= ?
           ORDER BY shift.shift_date, shift.start_time""",
        (flask.g.venue["id"], uk_today().isoformat()),
    ).fetchall()
    return flask.render_template("staff/open_shifts.html", shifts=rows)


@staff_bp.route("/open-shifts/<int:shift_id>")
@require_permission("staff", "rota_admin", "app_admin")
def claim_open_shift_prompt(shift_id):
    db = get_db()
    shift_row = db.execute(
        "SELECT * FROM shift WHERE id = ? AND venue_id = ? AND status = 'open'", (shift_id, flask.g.venue["id"])
    ).fetchone()
    if shift_row is None:
        flask.flash("That shift has already been claimed.", "error")
        return flask.redirect(flask.url_for("staff_portal.open_shifts"))
    return flask.render_template("staff/claim_prompt.html", shift=shift_row)


@staff_bp.route("/open-shifts/<int:shift_id>/claim", methods=["POST"])
@require_permission("staff", "rota_admin", "app_admin")
def claim_open_shift(shift_id):
    db = get_db()
    # Atomic, first-tap-wins (spec §5.4): the UPDATE's own WHERE clause is
    # the compare-and-swap — SQLite's single-writer serialization makes a
    # rowcount of 1 a reliable "I won the claim" signal with no extra locking.
    cur = db.execute(
        "UPDATE shift SET status = 'scheduled', person_id = ? WHERE id = ? AND venue_id = ? AND status = 'open'",
        (flask.g.person["id"], shift_id, flask.g.venue["id"]),
    )
    db.commit()
    if cur.rowcount == 1:
        shift_row = db.execute("SELECT shift_date, start_time, end_time FROM shift WHERE id = ?", (shift_id,)).fetchone()
        notify_admins(
            db, flask.g.venue, "open_shift_claimed",
            f"Open shift claimed — {flask.g.venue['name']}",
            f"{flask.g.person['name']} claimed the open shift on {shift_row['shift_date']} "
            f"{shift_row['start_time']}-{shift_row['end_time']} at {flask.g.venue['name']}.",
        )
        flask.flash("Shift claimed — it's yours.")
    else:
        flask.flash("Someone else already claimed that shift.", "error")
    return flask.redirect(flask.url_for("staff_portal.open_shifts"))


# ---------- Shift swaps (spec §5.5) ----------


@staff_bp.route("/shift/<int:shift_id>/swap/request", methods=["POST"])
@require_permission("staff", "rota_admin", "app_admin")
def request_swap(shift_id):
    db = get_db()
    to_person_id = flask.request.form.get("to_person_id", type=int)
    shift_row = db.execute(
        "SELECT * FROM shift WHERE id = ? AND venue_id = ? AND person_id = ?",
        (shift_id, flask.g.venue["id"], flask.g.person["id"]),
    ).fetchone()
    if shift_row is None or not to_person_id:
        flask.abort(400)
    # The swap target must be an active member of THIS venue — otherwise a
    # swap could be pointed at a person_id belonging to another venue.
    to_is_staff_here = db.execute(
        "SELECT 1 FROM venue_membership WHERE person_id = ? AND venue_id = ? AND status = 'active'",
        (to_person_id, flask.g.venue["id"]),
    ).fetchone()
    if to_is_staff_here is None:
        flask.abort(404)
    db.execute(
        "INSERT INTO shift_swap_request (shift_id, from_person_id, to_person_id) VALUES (?, ?, ?)",
        (shift_id, flask.g.person["id"], to_person_id),
    )
    db.commit()
    flask.flash("Swap requested — waiting for them to accept.")
    return flask.redirect(flask.url_for("staff_portal.shift_detail", shift_id=shift_id))


@staff_bp.route("/swaps")
@require_permission("staff", "rota_admin", "app_admin")
def my_swaps():
    db = get_db()
    person = flask.g.person
    incoming = db.execute(
        """SELECT shift_swap_request.*, shift.shift_date, shift.start_time, shift.end_time, person.name AS from_name
           FROM shift_swap_request
           JOIN shift ON shift.id = shift_swap_request.shift_id
           JOIN person ON person.id = shift_swap_request.from_person_id
           WHERE shift_swap_request.to_person_id = ? AND shift_swap_request.status = 'pending_peer'""",
        (person["id"],),
    ).fetchall()
    return flask.render_template("staff/swaps.html", incoming=incoming)


@staff_bp.route("/swap/<int:swap_id>/accept", methods=["POST"])
@require_permission("staff", "rota_admin", "app_admin")
def accept_swap(swap_id):
    db = get_db()
    swap_row = db.execute(
        "SELECT * FROM shift_swap_request WHERE id = ? AND to_person_id = ? AND status = 'pending_peer'",
        (swap_id, flask.g.person["id"]),
    ).fetchone()
    if swap_row is None:
        flask.abort(404)
    db.execute(
        "UPDATE shift_swap_request SET status = 'pending_admin', peer_responded_at = datetime('now') WHERE id = ?",
        (swap_id,),
    )
    db.commit()
    # Notified here, not at the initial request — this is the point it
    # actually needs an admin's attention (peer has accepted, awaiting
    # approval); the earlier staff-to-peer request has no admin role yet.
    shift_row = db.execute(
        "SELECT shift_date, start_time, end_time FROM shift WHERE id = ?", (swap_row["shift_id"],)
    ).fetchone()
    notify_admins(
        db, flask.g.venue, "swap_request",
        f"Shift swap needs approval — {flask.g.venue['name']}",
        f"{flask.g.person['name']} has accepted a shift swap for {shift_row['shift_date']} "
        f"{shift_row['start_time']}-{shift_row['end_time']} at {flask.g.venue['name']} — it now needs your approval.",
    )
    flask.flash("Accepted — waiting for admin approval to finalise.")
    return flask.redirect(flask.url_for("staff_portal.my_swaps"))


@staff_bp.route("/swap/<int:swap_id>/decline", methods=["POST"])
@require_permission("staff", "rota_admin", "app_admin")
def decline_swap_peer(swap_id):
    db = get_db()
    db.execute(
        "UPDATE shift_swap_request SET status = 'declined', peer_responded_at = datetime('now') WHERE id = ? AND to_person_id = ?",
        (swap_id, flask.g.person["id"]),
    )
    db.commit()
    return flask.redirect(flask.url_for("staff_portal.my_swaps"))
