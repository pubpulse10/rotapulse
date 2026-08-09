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
from app.db import get_db, get_app_id
from app.extensions import limiter
from app.notification_settings import check_missed_clock_ins, check_missed_clock_outs

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
    return jsonify({"missed_clock_in": missed_in, "missed_clock_out": missed_out})


@internal_bp.route("/access", methods=["POST"])
@limiter.limit("60 per minute")
def access():
    """Phase 2: the Hub pushes a staff person's RotaPulse grant here whenever
    it changes. Materialises a person (keyed by person.hub_person_id) + an
    active venue_membership + one app_access at the mapped level. Hub
    'manager' -> rota_admin, 'staff' -> staff. status 'inactive' -> app_access
    'revoked' (locked out, history kept). This runs alongside RotaPulse's own
    (now dormant) invite/login system — retired in Phase 4."""
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
    email = data.get("email") or ""
    perm = "rota_admin" if data.get("level") == "manager" else "staff"
    aa_status = "active" if data.get("status") == "active" else "revoked"

    # Person keyed by hub_person_id; pub_id stays NULL (that's owner-only).
    person = db.execute("SELECT id FROM person WHERE hub_person_id = ?", (hub_person_id,)).fetchone()
    if person is None:
        cur = db.execute("INSERT INTO person (name, email, hub_person_id) VALUES (?, ?, ?)",
                         (name, email, hub_person_id))
        person_id = cur.lastrowid
    else:
        person_id = person["id"]
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
    return jsonify({"ok": True})
