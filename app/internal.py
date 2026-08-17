"""
Server-to-server endpoint for TaskPulse's cross-app integration (spec §10's
strongest differentiator): "a person clocked in via RotaPulse is
automatically recognised by TaskPulse's staff-selection step."

TaskPulse already knows *which* person via the shared session cookie
(session['rotapulse_person_id'], readable there because it shares the same
SECRET_KEY/SESSION_COOKIE_DOMAIN — no token exchange needed for that part).
What it can't know on its own is whether that person is *currently clocked
in right now* — that's what this endpoint answers, authenticated the same
bearer-secret way as the sibling apps' existing /internal/* calls.
"""

import hmac
from datetime import datetime

from flask import Blueprint, jsonify, request

from app import config
from app.billing import enforce_band
from app.db import get_db, get_app_id, delete_venue_by_pub_id
from app.extensions import limiter
from app.notification_settings import check_missed_clock_ins, check_missed_clock_outs, remind_staff_to_clock_in

internal_bp = Blueprint("internal", __name__, url_prefix="/internal")


def _authorized():
    # Fail closed when the secret is unset, and compare in constant time so a
    # caller can't recover the secret byte-by-byte via response timing.
    secret = config.INTERNAL_API_SECRET
    if not secret:
        return False
    provided = request.headers.get("Authorization", "")
    return hmac.compare_digest(provided, f"Bearer {secret}")


@internal_bp.route("/clock-status", methods=["POST"])
@limiter.limit("60 per minute")
def clock_status():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    pub_id = data.get("pub_id")
    person_id = data.get("person_id")
    if pub_id is None or person_id is None:
        return jsonify({"error": "pub_id and person_id are required"}), 400

    db = get_db()
    venue = db.execute("SELECT id FROM venue WHERE pub_id = ?", (pub_id,)).fetchone()
    if venue is None:
        return jsonify({"clocked_in": False})

    row = db.execute(
        """SELECT person.name, person.avatar_url FROM attendance
           JOIN shift ON shift.id = attendance.shift_id
           JOIN person ON person.id = shift.person_id
           WHERE shift.venue_id = ? AND shift.person_id = ? AND attendance.clock_out_at IS NULL""",
        (venue["id"], person_id),
    ).fetchone()
    if row is None:
        return jsonify({"clocked_in": False})

    return jsonify({"clocked_in": True, "person_id": person_id, "name": row["name"], "avatar_url": row["avatar_url"]})


@internal_bp.route("/run-shift-notifications", methods=["POST"])
@limiter.limit("60 per minute")
def run_shift_notifications():
    """Called by the Render Cron Job (scripts/check_shift_notifications.py has
    no persistent disk of its own to read the DB from, so it hits this
    endpoint over HTTP instead — the web service's connection is the one
    with the real, disk-backed database)."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    db = get_db()
    now = datetime.now()
    missed_in = check_missed_clock_ins(db, now)
    missed_out = check_missed_clock_outs(db, now)
    staff_reminders = remind_staff_to_clock_in(db, now)
    return jsonify({
        "missed_clock_in": missed_in, "missed_clock_out": missed_out,
        "staff_clock_in_reminders": staff_reminders,
    })


def _link_or_create_person(db, hub_person_id, name, email):
    """Resolves the Hub-pushed person to a local row — by hub_person_id if
    already linked, else by email against a person RotaPulse's OWN invite
    flow (app/admin_config.py::create_staff) already created locally with
    no hub_person_id at all. Without the email fallback, inviting the same
    real human through both paths would create two disconnected person
    records instead of recognising them as one — confirmed possible
    2026-08-14 since both invite paths are live simultaneously with no
    reconciliation. Only matches a still-unlinked person (hub_person_id IS
    NULL), so this can never steal/overwrite a different person's existing
    Hub link."""
    person = db.execute("SELECT id FROM person WHERE hub_person_id = ?", (hub_person_id,)).fetchone()
    if person is not None:
        return person["id"]

    if email:
        person = db.execute(
            "SELECT id FROM person WHERE hub_person_id IS NULL AND email = ?", (email,)
        ).fetchone()
        if person is not None:
            db.execute("UPDATE person SET hub_person_id = ? WHERE id = ?", (hub_person_id, person["id"]))
            return person["id"]

    cur = db.execute(
        "INSERT INTO person (name, email, hub_person_id) VALUES (?, ?, ?)", (name, email, hub_person_id)
    )
    return cur.lastrowid


@internal_bp.route("/access", methods=["POST"])
@limiter.limit("60 per minute")
def access():
    """Phase 2: the Hub pushes a staff person's RotaPulse grant here whenever
    it changes. Materialises a person (keyed by person.hub_person_id) + an
    active venue_membership + one app_access at the mapped level. Hub
    'manager' -> rota_admin, 'staff' -> staff. status 'inactive' -> app_access
    'revoked' (locked out, history kept). This runs alongside RotaPulse's own
    invite/login system (app/rota_login.py, app/admin_config.py) — despite
    once being described as "dormant" here, it's still fully live and is
    genuinely how staff get invited today; the two were never reconciled.
    See _link_or_create_person for how that's kept from producing duplicate
    person records for the same human."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    pub_id = data.get("pub_id")
    hub_person_id = data.get("person_id")
    if pub_id is None or hub_person_id is None:
        return jsonify({"error": "pub_id and person_id are required"}), 400

    db = get_db()
    venue = db.execute("SELECT id FROM venue WHERE pub_id = ?", (pub_id,)).fetchone()
    if venue is None:
        return jsonify({"ok": True})   # no venue yet -> nothing to attach to
    venue_id = venue["id"]
    name = data.get("name") or ""
    # Lowercased to match app/admin_config.py::create_staff's own convention
    # (email = form.get("email", "").strip().lower() or None) — otherwise a
    # case difference between the two invite paths would defeat the
    # email-reconciliation match in _link_or_create_person below.
    email = (data.get("email") or "").strip().lower()
    perm = "rota_admin" if data.get("level") == "manager" else "staff"
    aa_status = "active" if data.get("status") == "active" else "revoked"

    # pub_id stays NULL on this person row (that's owner-only).
    person_id = _link_or_create_person(db, hub_person_id, name, email)
    db.execute("UPDATE person SET name = ?, email = ? WHERE id = ?", (name, email, person_id))

    membership = db.execute(
        "SELECT id FROM venue_membership WHERE person_id = ? AND venue_id = ?",
        (person_id, venue_id)).fetchone()
    if membership is None:
        cur = db.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')",
            (person_id, venue_id))
        membership_id = cur.lastrowid
    else:
        membership_id = membership["id"]
        db.execute("UPDATE venue_membership SET status = 'active' WHERE id = ?", (membership_id,))

    app_id = get_app_id(db, "rotapulse")
    # One current RotaPulse grant per membership: clear any prior level, set new.
    db.execute("DELETE FROM app_access WHERE venue_membership_id = ? AND app_id = ?",
               (membership_id, app_id))
    db.execute(
        "INSERT INTO app_access (venue_membership_id, app_id, permission_level, status, accepted_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (membership_id, app_id, perm, aa_status))
    db.commit()
    # Adding an active grant via the Hub can push the venue into a higher
    # billing band, but the Hub path never went through the local
    # invite/approve flow that calls enforce_band — so re-evaluate here too,
    # otherwise staff added via the Hub would underpay. Safe no-op when the
    # venue has no live Stripe subscription (still on trial / Stripe not
    # configured), and moves the band in whichever direction now fits,
    # prorated (mirrors the local flow).
    enforce_band(venue_id)
    return jsonify({"ok": True})


@internal_bp.route("/venues/delete", methods=["POST"])
@limiter.limit("60 per minute")
def venues_delete():
    """PricePulse calls this when a family account is deleted, so RotaPulse's
    own venue data for that pub_id is cleaned up automatically instead of
    needing scripts/delete_test_venues.py run by hand via Render Shell."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    pub_id = data.get("pub_id")
    if pub_id is None:
        return jsonify({"error": "pub_id is required"}), 400

    db = get_db()
    deleted = delete_venue_by_pub_id(db, pub_id)
    return jsonify({"ok": True, "deleted": deleted})
