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

from flask import Blueprint, jsonify, request

from app import config
from app.db import get_db

internal_bp = Blueprint("internal", __name__, url_prefix="/internal")


def _authorized():
    expected = f"Bearer {config.INTERNAL_API_SECRET}"
    return bool(config.INTERNAL_API_SECRET) and request.headers.get("Authorization") == expected


@internal_bp.route("/clock-status", methods=["POST"])
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
