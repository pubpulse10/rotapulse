"""
The mobile-first, staff-facing side of RotaPulse (spec §5.3): a simple
day-by-day scrollable list of the logged-in person's own shifts, clock-
in/out, leave requests, and open-shift claiming. Available to all three
permission tiers (an app_admin/rota_admin who is also a working staff
member uses this same view for their own shifts).
"""

from datetime import date, datetime, timedelta

import flask

from app.db import get_db
from app.geo_distance import distance_metres
from app.leave import days_taken_count
from app.media import save_attendance_photo
from app.rota_auth import register_identity, require_permission
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
    today = date.today()
    horizon = today + timedelta(days=21)
    shifts = db.execute(
        """SELECT shift.*, attendance.clock_in_at, attendance.clock_out_at
           FROM shift LEFT JOIN attendance ON attendance.shift_id = shift.id
           WHERE shift.venue_id = ? AND shift.person_id = ? AND shift.status = 'scheduled'
           AND shift.shift_date BETWEEN ? AND ?
           ORDER BY shift.shift_date, shift.start_time""",
        (venue["id"], person["id"], today.isoformat(), horizon.isoformat()),
    ).fetchall()
    return flask.render_template("staff/home.html", shifts=shifts, today=today.isoformat())


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
    return flask.render_template("staff/shift_detail.html", shift=shift_row, attendance=attendance)


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

    variance_flag = 1 if _minutes_late(shift_row["start_time"]) > VARIANCE_THRESHOLD_MINUTES else 0

    db.execute(
        """INSERT INTO attendance
           (shift_id, clock_in_at, clock_in_lat, clock_in_lng, clock_in_location_confirmed, photo_url, variance_flag)
           VALUES (?, datetime('now'), ?, ?, ?, ?, ?)
           ON CONFLICT(shift_id) DO UPDATE SET clock_in_at = excluded.clock_in_at,
           clock_in_lat = excluded.clock_in_lat, clock_in_lng = excluded.clock_in_lng,
           clock_in_location_confirmed = excluded.clock_in_location_confirmed,
           photo_url = COALESCE(excluded.photo_url, attendance.photo_url),
           variance_flag = excluded.variance_flag""",
        (shift_id, lat, lng, location_confirmed, photo_url, variance_flag),
    )
    db.commit()
    flask.flash("Clocked in." if location_confirmed != 0 else "Clocked in — location not confirmed.")
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

    end_variance = 1 if _minutes_late(shift_row["end_time"]) > VARIANCE_THRESHOLD_MINUTES else 0
    db.execute(
        """UPDATE attendance SET clock_out_at = datetime('now'), clock_out_lat = ?, clock_out_lng = ?,
           clock_out_location_confirmed = ?, variance_flag = MAX(variance_flag, ?) WHERE shift_id = ?""",
        (lat, lng, location_confirmed, end_variance, shift_id),
    )
    db.commit()
    flask.flash("Clocked out." if location_confirmed != 0 else "Clocked out — location not confirmed.")
    return flask.redirect(flask.url_for("staff_portal.shift_detail", shift_id=shift_id))


VARIANCE_THRESHOLD_MINUTES = 15


def _radius():
    from app import config

    return config.CLOCK_IN_RADIUS_METRES


def _minutes_late(planned_hhmm: str) -> float:
    """Minutes between now and the planned HH:MM, on today's date — a
    simple, adequate proxy for "materially different from plan" (spec §6.2)
    without needing to track a running clock client-side."""
    planned_h, planned_m = map(int, planned_hhmm.split(":"))
    planned = datetime.now().replace(hour=planned_h, minute=planned_m, second=0, microsecond=0)
    return abs((datetime.now() - planned).total_seconds() / 60)


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
        (flask.g.venue["id"], date.today().isoformat()),
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
