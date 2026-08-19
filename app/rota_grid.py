"""
The rota week grid (spec §5), open/claimable shifts (§5.4), shift swaps
(§5.5), leave approval, event tags (§10), and turnover figures (§11) —
the app_admin/rota_admin-facing desktop-first view.

Cell precedence (spec §5.1), checked in this order for each staff x date:
  1. Approved leave covering that date -> palm tree
  2. A scheduled shift for that person on that date -> shift time + avatar
  3. An ad-hoc day-off override, OR the person's regular availability
     pattern says they don't work that weekday -> bed icon
  4. Otherwise -> dashed "+" (unrostered/empty)
Open/claimable shifts (no person_id) are rendered separately, per spec §5.4.
"""

import json
from datetime import date, timedelta

import flask

from app.costs import predicted_cost
from app.date_format import format_uk_date
from app.db import get_db
from app.notifications import send_email, send_sms
from app.rota_auth import register_identity, require_permission
from app.venue_scope import register_venue_gate, register_venue_scope
from app.weather import get_week_forecast

rota_bp = flask.Blueprint("rota_grid", __name__, url_prefix="/v/<slug>/rota")
register_venue_scope(rota_bp)
register_venue_gate(rota_bp)
register_identity(rota_bp)

WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _week_dates(week_start: date) -> list[date]:
    return [week_start + timedelta(days=i) for i in range(7)]


def _monday_of(on_date: date) -> date:
    return on_date - timedelta(days=on_date.weekday())


def _billable_staff(db, venue_id):
    return db.execute(
        """SELECT venue_membership.id AS membership_id, person.id AS person_id, person.name,
                  person.avatar_url, rota_staff_detail.availability
           FROM venue_membership
           JOIN person ON person.id = venue_membership.person_id
           JOIN rota_staff_detail ON rota_staff_detail.venue_membership_id = venue_membership.id
           WHERE venue_membership.venue_id = ? AND venue_membership.status = 'active'
           ORDER BY person.name""",
        (venue_id,),
    ).fetchall()


def _is_on_approved_leave(db, person_id, on_date_str):
    return db.execute(
        """SELECT 1 FROM leave_request WHERE person_id = ? AND status = 'approved'
           AND start_date <= ? AND end_date >= ?""",
        (person_id, on_date_str, on_date_str),
    ).fetchone() is not None


def _monday_options(db, venue_id: int, current_week_start: date, weeks_back: int = 8, weeks_forward: int = 52) -> list[dict]:
    """Every Monday a week could be copied into — a plain <select> of only
    Mondays, rather than a native date picker, since a week always starts
    on the day the pub's rota runs from (Monday for most pubs) and pasting
    onto any other day of the week isn't a valid target. Listing only
    Mondays achieves "grey out the other days" without needing a custom
    calendar widget. The currently-viewed week is left out — copying a
    week into itself is already rejected server-side, so there's no point
    offering it.

    Each option also carries has_shifts, so the page can warn before a
    paste onto a week that isn't empty — computed here, in one query
    covering the whole listed range, rather than one query per option."""
    range_start = (current_week_start - timedelta(weeks=weeks_back)).isoformat()
    range_end = (current_week_start + timedelta(weeks=weeks_forward) + timedelta(days=6)).isoformat()
    shift_dates = {
        row["shift_date"]
        for row in db.execute(
            "SELECT DISTINCT shift_date FROM shift WHERE venue_id = ? AND shift_date BETWEEN ? AND ?",
            (venue_id, range_start, range_end),
        ).fetchall()
    }

    options = []
    for offset in range(-weeks_back, weeks_forward + 1):
        monday = current_week_start + timedelta(weeks=offset)
        if monday == current_week_start:
            continue
        week_dates = {(monday + timedelta(days=i)).isoformat() for i in range(7)}
        options.append({
            "value": monday.isoformat(),
            "label": f"{monday.day} {monday.strftime('%B %Y')}",
            "has_shifts": bool(shift_dates & week_dates),
        })
    return options


@rota_bp.route("/")
@require_permission("app_admin", "rota_admin")
def week():
    db = get_db()
    venue = flask.g.venue
    week_param = flask.request.args.get("week")
    week_start = _monday_of(date.fromisoformat(week_param)) if week_param else _monday_of(date.today())
    dates = _week_dates(week_start)
    date_strs = [d.isoformat() for d in dates]

    week_notification = db.execute(
        "SELECT * FROM week_notification WHERE venue_id = ? AND week_start_date = ?",
        (venue["id"], week_start.isoformat()),
    ).fetchone()

    staff = _billable_staff(db, venue["id"])
    shift_rows = db.execute(
        """SELECT shift.*, attendance.approval_status FROM shift
           LEFT JOIN attendance ON attendance.shift_id = shift.id
           WHERE shift.venue_id = ? AND shift.shift_date BETWEEN ? AND ? AND shift.status = 'scheduled'""",
        (venue["id"], date_strs[0], date_strs[-1]),
    ).fetchall()
    pending_approval_count = db.execute(
        """SELECT COUNT(*) AS n FROM attendance JOIN shift ON shift.id = attendance.shift_id
           WHERE shift.venue_id = ? AND attendance.approval_status = 'pending'""",
        (venue["id"],),
    ).fetchone()["n"]
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
    roles = db.execute("SELECT * FROM venue_role WHERE venue_id = ? ORDER BY name", (venue["id"],)).fetchall()
    open_shifts_by_date = {}
    for s in open_shifts:
        open_shifts_by_date.setdefault(s["shift_date"], []).append(s)

    event_tags = db.execute(
        "SELECT * FROM event_tag WHERE venue_id = ? AND tag_date BETWEEN ? AND ?",
        (venue["id"], date_strs[0], date_strs[-1]),
    ).fetchall()
    tags_by_date = {}
    for t in event_tags:
        tags_by_date.setdefault(t["tag_date"], []).append({"id": t["id"], "label": t["label"]})

    forecast = get_week_forecast(venue["id"], venue["latitude"], venue["longitude"], date_strs)
    daily_cost = {d_str: predicted_cost(venue["id"], d_str, d_str) for d_str in date_strs}
    week_cost = predicted_cost(venue["id"], date_strs[0], date_strs[-1])

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
                cell = {"state": "day_off", "override": True}
            elif not availability.get(WEEKDAY_KEYS[d.weekday()], True):
                cell = {"state": "day_off", "override": False}
            else:
                cell = {"state": "empty"}
            row_cells.append(cell)
        grid.append({"member": member, "cells": row_cells})

    return flask.render_template(
        "rota/week.html",
        venue=venue,
        week_start=week_start,
        dates=dates,
        date_strs=date_strs,
        grid=grid,
        open_shifts=open_shifts,
        open_shifts_by_date=open_shifts_by_date,
        roles=roles,
        tags_by_date=tags_by_date,
        forecast=forecast,
        daily_cost=daily_cost,
        monday_options=_monday_options(db, venue["id"], week_start),
        week_cost=week_cost,
        week_notification=week_notification,
        pending_approval_count=pending_approval_count,
        prev_week=(week_start - timedelta(days=7)).isoformat(),
        next_week=(week_start + timedelta(days=7)).isoformat(),
    )


@rota_bp.route("/copy-week", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def copy_week():
    """Copies every shift (scheduled and open) from the currently-viewed
    week onto another week — not necessarily the next one, any week the
    admin picks. Each shift keeps its same day-of-week offset, person,
    role, and time. Skips (rather than overwrites) any target slot that
    would conflict — the target person already has a shift that day, or is
    on approved leave — same conflict rules as dragging a shift on the
    grid, so a copy can never silently double-book or combine shifts."""
    db = get_db()
    venue_id = flask.g.venue["id"]
    form = flask.request.form
    source_week_start = _monday_of(date.fromisoformat(form["source_week"]))
    target_week_start = _monday_of(date.fromisoformat(form["target_week"]))

    if source_week_start == target_week_start:
        flask.flash("Pick a different week to copy into.", "error")
        return flask.redirect(flask.url_for("rota_grid.week", week=source_week_start.isoformat()))

    source_dates = _week_dates(source_week_start)
    source_shifts = db.execute(
        "SELECT * FROM shift WHERE venue_id = ? AND shift_date BETWEEN ? AND ?",
        (venue_id, source_dates[0].isoformat(), source_dates[-1].isoformat()),
    ).fetchall()

    if not source_shifts:
        flask.flash("No shifts in that week to copy.", "error")
        return flask.redirect(flask.url_for("rota_grid.week", week=source_week_start.isoformat()))

    copied = 0
    skipped = 0
    for s in source_shifts:
        offset_days = (date.fromisoformat(s["shift_date"]) - source_week_start).days
        target_date = (target_week_start + timedelta(days=offset_days)).isoformat()

        if s["person_id"] is not None:
            on_leave = db.execute(
                """SELECT 1 FROM leave_request WHERE person_id = ? AND status = 'approved'
                   AND start_date <= ? AND end_date >= ?""",
                (s["person_id"], target_date, target_date),
            ).fetchone()
            already_has_shift = db.execute(
                "SELECT 1 FROM shift WHERE venue_id = ? AND person_id = ? AND shift_date = ? AND status = 'scheduled'",
                (venue_id, s["person_id"], target_date),
            ).fetchone()
            if on_leave is not None or already_has_shift is not None:
                skipped += 1
                continue

        db.execute(
            """INSERT INTO shift (venue_id, person_id, venue_role_id, shift_date, start_time, end_time, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (venue_id, s["person_id"], s["venue_role_id"], target_date, s["start_time"], s["end_time"], s["status"]),
        )
        copied += 1

    db.commit()
    message = f"Copied {copied} shift(s) to the week of {target_week_start.isoformat()}."
    if skipped:
        message += f" Skipped {skipped} that would have conflicted with an existing shift or approved leave."
    flask.flash(message)
    return flask.redirect(flask.url_for("rota_grid.week", week=target_week_start.isoformat()))


@rota_bp.route("/clear-week", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def clear_week():
    """Deletes every shift (scheduled and open) in the currently-viewed
    week — a fast reset when a week needs rebuilding from scratch. Scoped
    to shifts only, same as copy_week: leave requests, day-off overrides,
    and event tags are untouched, since those aren't part of "the rota" in
    the same throwaway-and-rebuild sense a shift is. Any attendance
    records tied to a cleared shift go with it — there's no plan left to
    have clocked in against."""
    db = get_db()
    venue_id = flask.g.venue["id"]
    week_start = _monday_of(date.fromisoformat(flask.request.form["week"]))
    week_end = week_start + timedelta(days=6)

    db.execute(
        """DELETE FROM attendance WHERE shift_id IN
           (SELECT id FROM shift WHERE venue_id = ? AND shift_date BETWEEN ? AND ?)""",
        (venue_id, week_start.isoformat(), week_end.isoformat()),
    )
    cur = db.execute(
        "DELETE FROM shift WHERE venue_id = ? AND shift_date BETWEEN ? AND ?",
        (venue_id, week_start.isoformat(), week_end.isoformat()),
    )
    db.commit()
    flask.flash(f"Cleared {cur.rowcount} shift(s) from the week of {week_start.day} {week_start.strftime('%B %Y')}.")
    return flask.redirect(flask.url_for("rota_grid.week", week=week_start.isoformat()))


@rota_bp.route("/notify-week", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def notify_week():
    """Pushes each staff member their shift times for the currently-viewed
    week, once only — the whole point of the unnotified/notified status is
    that staff don't get re-spammed every time the admin re-visits the
    week. week_notification's PRIMARY KEY (venue_id, week_start_date)
    enforces that at the data level; this check is what turns "would
    violate that" into a friendly flash instead of a 500."""
    db = get_db()
    venue = flask.g.venue
    week_start = _monday_of(date.fromisoformat(flask.request.form["week"]))
    week_end = week_start + timedelta(days=6)

    already_notified = db.execute(
        "SELECT 1 FROM week_notification WHERE venue_id = ? AND week_start_date = ?",
        (venue["id"], week_start.isoformat()),
    ).fetchone()
    if already_notified is not None:
        flask.flash("This week has already been notified — it can't be sent again.", "error")
        return flask.redirect(flask.url_for("rota_grid.week", week=week_start.isoformat()))

    shift_rows = db.execute(
        """SELECT shift.shift_date, shift.start_time, shift.end_time, person.id AS person_id,
                  person.name, person.email, person.mobile
           FROM shift JOIN person ON person.id = shift.person_id
           WHERE shift.venue_id = ? AND shift.status = 'scheduled'
           AND shift.shift_date BETWEEN ? AND ?
           ORDER BY person.name, shift.shift_date, shift.start_time""",
        (venue["id"], week_start.isoformat(), week_end.isoformat()),
    ).fetchall()

    if not shift_rows:
        flask.flash("No scheduled shifts to notify for this week.", "error")
        return flask.redirect(flask.url_for("rota_grid.week", week=week_start.isoformat()))

    shifts_by_person = {}
    for row in shift_rows:
        shifts_by_person.setdefault(row["person_id"], {"row": row, "shifts": []})["shifts"].append(row)

    week_label = f"{week_start.day} {week_start.strftime('%B %Y')}"
    notified = 0
    for entry in shifts_by_person.values():
        person = entry["row"]
        lines = [f"Your shifts for the week of {week_label}:"]
        for s in entry["shifts"]:
            shift_date = date.fromisoformat(s["shift_date"])
            lines.append(f"{shift_date.strftime('%a')} {shift_date.day} {shift_date.strftime('%b')}: {s['start_time']}-{s['end_time']}")
        message = "\n".join(lines)

        if person["email"]:
            send_email(person["email"], f"Your shifts for the week of {week_label}", message)
        if person["mobile"]:
            send_sms(person["mobile"], message)
        if person["email"] or person["mobile"]:
            notified += 1

    db.execute(
        "INSERT INTO week_notification (venue_id, week_start_date, notified_by_person_id, recipient_count) VALUES (?, ?, ?, ?)",
        (venue["id"], week_start.isoformat(), flask.g.person["id"] if flask.g.person else None, notified),
    )
    db.commit()
    flask.flash(f"Notified {notified} staff member(s) about their shifts for the week of {week_label}.")
    return flask.redirect(flask.url_for("rota_grid.week", week=week_start.isoformat()))


@rota_bp.route("/cell/<int:person_id>/<on_date>")
@require_permission("app_admin", "rota_admin")
def cell(person_id, on_date):
    db = get_db()
    venue = flask.g.venue
    person = db.execute("SELECT * FROM person WHERE id = ?", (person_id,)).fetchone()
    # LEFT JOIN attendance so the panel can show the actual clock-in/out
    # time next to the scheduled one — previously only ever shown to the
    # staff member themselves (app/staff_portal.py), never to an admin.
    shifts = db.execute(
        """SELECT shift.*, attendance.clock_in_at, attendance.clock_out_at,
                  attendance.variance_flag, attendance.clock_in_location_confirmed,
                  attendance.clock_out_location_confirmed, attendance.approval_status
           FROM shift
           LEFT JOIN attendance ON attendance.shift_id = shift.id
           WHERE shift.venue_id = ? AND shift.person_id = ? AND shift.shift_date = ?""",
        (venue["id"], person_id, on_date),
    ).fetchall()
    override = db.execute(
        "SELECT * FROM day_off_override WHERE venue_id = ? AND person_id = ? AND override_date = ?",
        (venue["id"], person_id, on_date),
    ).fetchone()
    roles = db.execute("SELECT * FROM venue_role WHERE venue_id = ? ORDER BY name", (venue["id"],)).fetchall()
    return flask.render_template(
        "rota/cell.html", person=person, on_date=on_date, shifts=shifts, override=override, roles=roles
    )


@rota_bp.route("/shift/create", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def create_shift():
    """Also doubles as "add a brand-new open shift" (spec §5.4) when no
    person_id is given — e.g. from the Open shifts panel, where there's no
    grid cell to tap since an open shift isn't tied to anyone yet. Avoids
    the old two-step workaround of creating a scheduled shift for someone
    and then immediately opening it back up."""
    db = get_db()
    form = flask.request.form
    person_id = form.get("person_id", type=int)
    # A posted person_id must be an active member of THIS venue — otherwise a
    # scheduled shift could be created against someone at another venue.
    if person_id is not None:
        is_staff_here = db.execute(
            "SELECT 1 FROM venue_membership WHERE person_id = ? AND venue_id = ? AND status = 'active'",
            (person_id, flask.g.venue["id"]),
        ).fetchone()
        if is_staff_here is None:
            flask.abort(404)
    status = "scheduled" if person_id else "open"
    db.execute(
        """INSERT INTO shift (venue_id, person_id, venue_role_id, shift_date, start_time, end_time, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            flask.g.venue["id"], person_id, form.get("venue_role_id", type=int) or None,
            form["shift_date"], form["start_time"], form["end_time"], status,
        ),
    )
    db.commit()
    return flask.redirect(flask.url_for("rota_grid.week", week=form.get("shift_date")))


@rota_bp.route("/shift/<int:shift_id>/update", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def update_shift(shift_id):
    db = get_db()
    form = flask.request.form
    db.execute(
        "UPDATE shift SET start_time = ?, end_time = ?, venue_role_id = ? WHERE id = ? AND venue_id = ?",
        (form["start_time"], form["end_time"], form.get("venue_role_id", type=int) or None, shift_id, flask.g.venue["id"]),
    )
    db.commit()
    return flask.redirect(flask.url_for("rota_grid.week", week=form.get("shift_date")))


@rota_bp.route("/shift/<int:shift_id>/move", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def move_shift(shift_id):
    """Drag-and-drop target (app/static/rota_dragdrop.js): reassigns a
    shift to a different person and/or date in one step — the same thing
    dropping it onto a different grid cell means. JSON in/out (not a form
    redirect like the other shift routes) since it's called from JS, not a
    page navigation.

    2026-08-19: also handles the Open row as a genuine drag target/source,
    not just person cells — dropping a scheduled shift there un-assigns it
    (status='open', same as the "Mark as open" button); dragging an open
    shift onto a person claims it for them (status='scheduled'), same as
    claim_open_shift but admin-initiated. The target cell's data-person-id
    is simply absent for the Open row (see week.html), so an absent
    person_id in the JSON body is what means "make it open" here — not a
    separate endpoint, since every other rule (attendance guard, leave
    conflict, one-shift-per-day) applies identically regardless of which
    direction the shift is moving."""
    db = get_db()
    venue_id = flask.g.venue["id"]
    data = flask.request.get_json(silent=True) or {}
    raw_person_id = data.get("person_id")
    new_date = data.get("shift_date")
    if not new_date:
        return flask.jsonify({"error": "Invalid target."}), 400

    new_person_id = None
    if raw_person_id not in (None, "", "null"):
        try:
            new_person_id = int(raw_person_id)
        except (TypeError, ValueError):
            return flask.jsonify({"error": "Invalid target."}), 400

    shift_row = db.execute(
        "SELECT * FROM shift WHERE id = ? AND venue_id = ? AND status IN ('scheduled', 'open')", (shift_id, venue_id)
    ).fetchone()
    if shift_row is None:
        return flask.jsonify({"error": "That shift no longer exists."}), 404

    # Real bug this guards against: attendance is keyed by shift_id, not by
    # date, so moving a shift that's already been clocked in/out for used to
    # silently carry that clock-in/out data along to the new person/date —
    # confirmed live, a shift moved to a new day kept showing "clocked in"
    # from several days earlier, which also made the missed-clock-in/staff
    # reminder checks wrongly think the (new) shift was already covered.
    # Attendance is a real historical + payroll-relevant record, so it's
    # blocked outright rather than silently discarded — moving is only ever
    # meant for a not-yet-worked shift. (An 'open' shift can never actually
    # have attendance — clock_in() requires a matching person_id, and an
    # open shift's is NULL — so this only ever bites the scheduled->open
    # direction in practice, but the check applies uniformly either way.)
    has_attendance = db.execute("SELECT 1 FROM attendance WHERE shift_id = ?", (shift_id,)).fetchone()
    if has_attendance is not None:
        return flask.jsonify({
            "error": "That shift already has clock-in/out recorded and can't be moved — "
                     "delete it and create a new shift instead if it needs to change.",
        }), 409

    if new_person_id is None:
        # Dropped onto the Open row — becomes (or stays) unassigned. Keeps
        # its venue_role_id untouched, which is what notify_open_shift's
        # role-targeted alert relies on.
        db.execute(
            "UPDATE shift SET person_id = NULL, status = 'open', shift_date = ? WHERE id = ? AND venue_id = ?",
            (new_date, shift_id, venue_id),
        )
        db.commit()
        return flask.jsonify({"ok": True})

    is_billable_staff = db.execute(
        """SELECT 1 FROM venue_membership
           JOIN rota_staff_detail ON rota_staff_detail.venue_membership_id = venue_membership.id
           WHERE venue_membership.person_id = ? AND venue_membership.venue_id = ? AND venue_membership.status = 'active'""",
        (new_person_id, venue_id),
    ).fetchone()
    if is_billable_staff is None:
        return flask.jsonify({"error": "That's not an active staff member here."}), 400

    on_leave = db.execute(
        """SELECT 1 FROM leave_request WHERE person_id = ? AND status = 'approved'
           AND start_date <= ? AND end_date >= ?""",
        (new_person_id, new_date, new_date),
    ).fetchone()
    if on_leave is not None:
        return flask.jsonify({"error": "That person is on approved leave that day."}), 409

    already_has_shift = db.execute(
        """SELECT 1 FROM shift WHERE venue_id = ? AND person_id = ? AND shift_date = ?
           AND status = 'scheduled' AND id != ?""",
        (venue_id, new_person_id, new_date, shift_id),
    ).fetchone()
    if already_has_shift is not None:
        return flask.jsonify({"error": "That person already has a shift that day."}), 409

    db.execute(
        "UPDATE shift SET person_id = ?, shift_date = ?, status = 'scheduled' WHERE id = ? AND venue_id = ?",
        (new_person_id, new_date, shift_id, venue_id),
    )
    db.commit()
    return flask.jsonify({"ok": True})


@rota_bp.route("/shift/<int:shift_id>/delete", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def delete_shift(shift_id):
    """Deletes a shift and everything else in the schema that references
    it by foreign key (attendance, any open-shift notification log
    entries, any shift-swap requests) — a shift being deletable at all
    means all of those are allowed to go with it; none of them make sense
    to keep pointing at a shift that no longer exists."""
    db = get_db()
    row = db.execute("SELECT shift_date FROM shift WHERE id = ? AND venue_id = ?", (shift_id, flask.g.venue["id"])).fetchone()
    db.execute("DELETE FROM attendance WHERE shift_id = ?", (shift_id,))
    db.execute("DELETE FROM shift_open_notification WHERE shift_id = ?", (shift_id,))
    db.execute("DELETE FROM shift_swap_request WHERE shift_id = ?", (shift_id,))
    db.execute("DELETE FROM shift WHERE id = ? AND venue_id = ?", (shift_id, flask.g.venue["id"]))
    db.commit()
    return flask.redirect(flask.url_for("rota_grid.week", week=row["shift_date"] if row else None))


@rota_bp.route("/day-off-override/create", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def create_override():
    db = get_db()
    form = flask.request.form
    db.execute(
        """INSERT INTO day_off_override (venue_id, person_id, override_date, note) VALUES (?, ?, ?, ?)
           ON CONFLICT(venue_id, person_id, override_date) DO NOTHING""",
        (flask.g.venue["id"], form.get("person_id", type=int), form["override_date"], form.get("note", "").strip() or None),
    )
    db.commit()
    return flask.redirect(flask.url_for("rota_grid.week", week=form.get("override_date")))


@rota_bp.route("/day-off-override/<int:override_id>/delete", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def delete_override(override_id):
    db = get_db()
    row = db.execute(
        "SELECT override_date FROM day_off_override WHERE id = ? AND venue_id = ?", (override_id, flask.g.venue["id"])
    ).fetchone()
    db.execute("DELETE FROM day_off_override WHERE id = ? AND venue_id = ?", (override_id, flask.g.venue["id"]))
    db.commit()
    return flask.redirect(flask.url_for("rota_grid.week", week=row["override_date"] if row else None))


# ---------- Open shifts (spec §5.4) ----------


@rota_bp.route("/open-shift/<int:shift_id>")
@require_permission("app_admin", "rota_admin")
def open_shift_panel(shift_id):
    """The tap target for an open-shift chip on the grid's "Open" row —
    the same one-consistent-panel pattern every other cell already uses
    (see cell() above), just for a shift that isn't tied to any person's
    row."""
    db = get_db()
    shift_row = db.execute(
        """SELECT shift.*, venue_role.name AS role_name FROM shift
           LEFT JOIN venue_role ON venue_role.id = shift.venue_role_id
           WHERE shift.id = ? AND shift.venue_id = ? AND shift.status = 'open'""",
        (shift_id, flask.g.venue["id"]),
    ).fetchone()
    if shift_row is None:
        flask.abort(404)
    return flask.render_template("rota/open_shift_panel.html", shift=shift_row)


@rota_bp.route("/open-shift/day/<on_date>")
@require_permission("app_admin", "rota_admin")
def open_shift_day(on_date):
    """The tap target for the Open row's cell on a given date — whether it
    already has open shifts or not — mirroring cell()'s role for a person's
    row: list what's there for this date, inline actions per shift, and an
    "add another" form pre-filled with the date. 2026-08-19, alongside
    making the Open row draggable like a real row, so a date with no open
    shifts yet is reachable directly from the grid instead of only via the
    separate form at the bottom of the page."""
    db = get_db()
    venue = flask.g.venue
    shifts = db.execute(
        """SELECT shift.*, venue_role.name AS role_name FROM shift
           LEFT JOIN venue_role ON venue_role.id = shift.venue_role_id
           WHERE shift.venue_id = ? AND shift.shift_date = ? AND shift.status = 'open'
           ORDER BY shift.start_time""",
        (venue["id"], on_date),
    ).fetchall()
    roles = db.execute("SELECT * FROM venue_role WHERE venue_id = ? ORDER BY name", (venue["id"],)).fetchall()
    return flask.render_template("rota/open_shift_day.html", on_date=on_date, shifts=shifts, roles=roles)


@rota_bp.route("/shift/<int:shift_id>/open", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def open_shift(shift_id):
    db = get_db()
    row = db.execute("SELECT shift_date FROM shift WHERE id = ? AND venue_id = ?", (shift_id, flask.g.venue["id"])).fetchone()
    db.execute(
        "UPDATE shift SET status = 'open', person_id = NULL WHERE id = ? AND venue_id = ?",
        (shift_id, flask.g.venue["id"]),
    )
    db.commit()
    return flask.redirect(flask.url_for("rota_grid.week", week=row["shift_date"] if row else None))


@rota_bp.route("/shift/<int:shift_id>/notify", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def notify_open_shift(shift_id):
    db = get_db()
    venue = flask.g.venue
    shift_row = db.execute(
        "SELECT * FROM shift WHERE id = ? AND venue_id = ? AND status = 'open'", (shift_id, venue["id"])
    ).fetchone()
    if shift_row is None:
        flask.abort(404)

    query = """SELECT DISTINCT person.mobile FROM venue_membership
               JOIN person ON person.id = venue_membership.person_id
               JOIN app_access ON app_access.venue_membership_id = venue_membership.id
               WHERE venue_membership.venue_id = ? AND venue_membership.status = 'active'
               AND app_access.status = 'active' AND person.mobile IS NOT NULL
               AND app_access.app_id = (SELECT id FROM app WHERE key = 'rotapulse')"""
    params = [venue["id"]]
    if shift_row["venue_role_id"]:
        query += " AND venue_membership.job_role_id = ?"
        params.append(shift_row["venue_role_id"])
    recipients = db.execute(query, params).fetchall()

    claim_url = flask.url_for("staff_portal.claim_open_shift_prompt", slug=flask.g.slug, shift_id=shift_id, _external=True)
    message = (
        f"Open shift at {venue['name']}: {format_uk_date(shift_row['shift_date'])} {shift_row['start_time']}-"
        f"{shift_row['end_time']}. First to claim gets it: {claim_url}"
    )
    for r in recipients:
        send_sms(r["mobile"], message)

    db.execute(
        "INSERT INTO shift_open_notification (shift_id, sent_by_person_id, recipient_count) VALUES (?, ?, ?)",
        (shift_id, flask.g.person["id"] if flask.g.person else None, len(recipients)),
    )
    db.commit()
    flask.flash(f"Notified {len(recipients)} staff about the open shift.")
    return flask.redirect(flask.url_for("rota_grid.week", week=shift_row["shift_date"]))


# ---------- Shift swaps (spec §5.5) ----------


@rota_bp.route("/swap/<int:swap_id>/approve", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def approve_swap(swap_id):
    db = get_db()
    venue_id = flask.g.venue["id"]
    # Scope the swap to the caller's venue by joining it to its shift — the
    # swap request row itself carries no venue_id, so without this join an
    # admin at one venue could approve another venue's swap by id (IDOR).
    swap_row = db.execute(
        """SELECT shift_swap_request.* FROM shift_swap_request
           JOIN shift ON shift.id = shift_swap_request.shift_id
           WHERE shift_swap_request.id = ? AND shift.venue_id = ?""",
        (swap_id, venue_id),
    ).fetchone()
    if swap_row is None or swap_row["status"] != "pending_admin":
        flask.abort(404)
    # The UPDATE also re-asserts venue_id so it can never touch another
    # venue's shift even if a swap row were somehow mis-pointed.
    db.execute(
        "UPDATE shift SET person_id = ? WHERE id = ? AND venue_id = ?",
        (swap_row["to_person_id"], swap_row["shift_id"], venue_id),
    )
    db.execute(
        "UPDATE shift_swap_request SET status = 'approved', admin_decided_at = datetime('now'), admin_decided_by_person_id = ? WHERE id = ?",
        (flask.g.person["id"] if flask.g.person else None, swap_id),
    )
    db.commit()
    flask.flash("Swap approved.")
    return flask.redirect(flask.url_for("rota_grid.week"))


@rota_bp.route("/swap/<int:swap_id>/decline", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def decline_swap(swap_id):
    db = get_db()
    venue_id = flask.g.venue["id"]
    # Scope to the caller's venue via the swap's shift (the swap row has no
    # venue_id) — 404 on a miss so another venue's swap can't be declined.
    cur = db.execute(
        """UPDATE shift_swap_request SET status = 'declined', admin_decided_at = datetime('now'),
           admin_decided_by_person_id = ?
           WHERE id = ? AND shift_id IN (SELECT id FROM shift WHERE venue_id = ?)""",
        (flask.g.person["id"] if flask.g.person else None, swap_id, venue_id),
    )
    if cur.rowcount == 0:
        flask.abort(404)
    db.commit()
    flask.flash("Swap declined.")
    return flask.redirect(flask.url_for("rota_grid.week"))


@rota_bp.route("/swaps")
@require_permission("app_admin", "rota_admin")
def swaps():
    db = get_db()
    rows = db.execute(
        """SELECT shift_swap_request.*, shift.shift_date, shift.start_time, shift.end_time,
                  from_person.name AS from_name, to_person.name AS to_name
           FROM shift_swap_request
           JOIN shift ON shift.id = shift_swap_request.shift_id
           JOIN person AS from_person ON from_person.id = shift_swap_request.from_person_id
           JOIN person AS to_person ON to_person.id = shift_swap_request.to_person_id
           WHERE shift.venue_id = ? AND shift_swap_request.status = 'pending_admin'
           ORDER BY shift_swap_request.requested_at""",
        (flask.g.venue["id"],),
    ).fetchall()
    return flask.render_template("rota/swaps.html", swaps=rows)


# ---------- Ad-hoc/early-start attendance approval (spec extension, 2026-08-18) ----------
# Two clock-in paths land here needing sign-off: a fully ad-hoc shift
# (app/staff_portal.py::start_ad_hoc_shift, shift.origin='ad_hoc') and an
# early start against a REAL rostered shift, more than
# staff_portal.EARLY_CLOCK_IN_GRACE_MINUTES before its planned start. Both
# just set attendance.approval_status='pending' — this doesn't care which
# case it is, it only acts on that column. Approval doesn't have to happen
# in real time (explicitly not urgent, per the request that shaped this) —
# it's the record of a decision that matters, not the timing of it.


@rota_bp.route("/approvals")
@require_permission("app_admin", "rota_admin")
def approvals_queue():
    db = get_db()
    rows = db.execute(
        """SELECT shift.*, attendance.clock_in_at, attendance.clock_out_at, person.name
           FROM attendance
           JOIN shift ON shift.id = attendance.shift_id
           JOIN person ON person.id = shift.person_id
           WHERE shift.venue_id = ? AND attendance.approval_status = 'pending'
           ORDER BY attendance.clock_in_at""",
        (flask.g.venue["id"],),
    ).fetchall()
    return flask.render_template("rota/approvals.html", rows=rows)


@rota_bp.route("/shift/<int:shift_id>/attendance/approve", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def approve_attendance(shift_id):
    db = get_db()
    # Scope to this venue via the shift join, same IDOR guard as approve_swap.
    row = db.execute(
        """SELECT attendance.id FROM attendance JOIN shift ON shift.id = attendance.shift_id
           WHERE attendance.shift_id = ? AND shift.venue_id = ? AND attendance.approval_status = 'pending'""",
        (shift_id, flask.g.venue["id"]),
    ).fetchone()
    if row is None:
        flask.abort(404)
    db.execute(
        """UPDATE attendance SET approval_status = 'approved', approval_decided_at = datetime('now'),
           approval_decided_by_person_id = ? WHERE shift_id = ?""",
        (flask.g.person["id"] if flask.g.person else None, shift_id),
    )
    db.commit()
    flask.flash("Approved.")
    return flask.redirect(flask.request.referrer or flask.url_for("rota_grid.approvals_queue"))


@rota_bp.route("/shift/<int:shift_id>/attendance/reject", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def reject_attendance(shift_id):
    db = get_db()
    row = db.execute(
        """SELECT attendance.id FROM attendance JOIN shift ON shift.id = attendance.shift_id
           WHERE attendance.shift_id = ? AND shift.venue_id = ? AND attendance.approval_status = 'pending'""",
        (shift_id, flask.g.venue["id"]),
    ).fetchone()
    if row is None:
        flask.abort(404)
    # The attendance record itself is kept, not deleted — an audit trail of
    # what was actually claimed, even once rejected (see payroll.py, which
    # excludes rejected rows from the pay totals but doesn't erase them).
    db.execute(
        """UPDATE attendance SET approval_status = 'rejected', approval_decided_at = datetime('now'),
           approval_decided_by_person_id = ? WHERE shift_id = ?""",
        (flask.g.person["id"] if flask.g.person else None, shift_id),
    )
    db.commit()
    flask.flash("Rejected — excluded from payroll. The record is kept for reference.")
    return flask.redirect(flask.request.referrer or flask.url_for("rota_grid.approvals_queue"))


# ---------- Leave approval ----------


@rota_bp.route("/leave")
@require_permission("app_admin", "rota_admin")
def leave_queue():
    db = get_db()
    rows = db.execute(
        """SELECT leave_request.*, person.name FROM leave_request
           JOIN person ON person.id = leave_request.person_id
           WHERE leave_request.venue_id = ? AND leave_request.status = 'pending'
           ORDER BY leave_request.requested_at""",
        (flask.g.venue["id"],),
    ).fetchall()
    staff = _billable_staff(db, flask.g.venue["id"])
    return flask.render_template("rota/leave_queue.html", requests=rows, staff=staff)


@rota_bp.route("/leave/create", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def create_leave():
    """Lets an admin record leave directly on a staff member's behalf —
    e.g. after a phone call — rather than only being able to approve leave
    staff submitted themselves. Since the admin is the same person who'd
    otherwise approve a self-submitted request, this goes straight to
    'approved' rather than sitting in the pending queue first."""
    db = get_db()
    venue_id = flask.g.venue["id"]
    form = flask.request.form
    person_id = form.get("person_id", type=int)
    start_date = form.get("start_date")
    end_date = form.get("end_date")

    is_staff_here = db.execute(
        """SELECT 1 FROM venue_membership WHERE person_id = ? AND venue_id = ? AND status = 'active'""",
        (person_id, venue_id),
    ).fetchone()
    if not person_id or not is_staff_here or not start_date or not end_date or start_date > end_date:
        flask.flash("Choose a staff member and a valid date range.", "error")
        return flask.redirect(flask.url_for("rota_grid.leave_queue"))

    db.execute(
        """INSERT INTO leave_request (person_id, venue_id, start_date, end_date, status, decided_at, decided_by_person_id)
           VALUES (?, ?, ?, ?, 'approved', datetime('now'), ?)""",
        (person_id, venue_id, start_date, end_date, flask.g.person["id"] if flask.g.person else None),
    )
    db.commit()
    flask.flash("Leave added.")
    return flask.redirect(flask.url_for("rota_grid.leave_queue"))


@rota_bp.route("/leave/<int:leave_id>/approve", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def approve_leave(leave_id):
    db = get_db()
    db.execute(
        "UPDATE leave_request SET status = 'approved', decided_at = datetime('now'), decided_by_person_id = ? WHERE id = ? AND venue_id = ?",
        (flask.g.person["id"] if flask.g.person else None, leave_id, flask.g.venue["id"]),
    )
    db.commit()
    return flask.redirect(flask.url_for("rota_grid.leave_queue"))


@rota_bp.route("/leave/<int:leave_id>/decline", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def decline_leave(leave_id):
    db = get_db()
    db.execute(
        "UPDATE leave_request SET status = 'declined', decided_at = datetime('now'), decided_by_person_id = ? WHERE id = ? AND venue_id = ?",
        (flask.g.person["id"] if flask.g.person else None, leave_id, flask.g.venue["id"]),
    )
    db.commit()
    return flask.redirect(flask.url_for("rota_grid.leave_queue"))


# ---------- Event tags (spec §10) ----------


@rota_bp.route("/event-tag/create", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def create_event_tag():
    db = get_db()
    form = flask.request.form
    label = form.get("label", "").strip()
    if label:
        db.execute(
            """INSERT INTO event_tag (venue_id, tag_date, label) VALUES (?, ?, ?)
               ON CONFLICT(venue_id, tag_date, label) DO NOTHING""",
            (flask.g.venue["id"], form["tag_date"], label),
        )
        db.commit()
    return flask.redirect(flask.url_for("rota_grid.week", week=form.get("tag_date")))


@rota_bp.route("/event-tag/<int:tag_id>/delete", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def delete_event_tag(tag_id):
    db = get_db()
    row = db.execute("SELECT tag_date FROM event_tag WHERE id = ? AND venue_id = ?", (tag_id, flask.g.venue["id"])).fetchone()
    db.execute("DELETE FROM event_tag WHERE id = ? AND venue_id = ?", (tag_id, flask.g.venue["id"]))
    db.commit()
    return flask.redirect(flask.url_for("rota_grid.week", week=row["tag_date"] if row else None))
