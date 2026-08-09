"""
Checks for shifts that have crossed the missed-clock-in or missed-clock-out
threshold (15 minutes late) and notifies whichever admins have opted in
(app/notification_settings.py). Intended to run every few minutes via an
external scheduler (Render Cron Job) - not auto-scheduled by the app
itself, same "run externally on a timer" pattern as
scripts/send_weekly_digest.py.

Assumes the server's local clock matches the venue's local time (no
timezone conversion — shift_date/start_time/end_time are stored as plain
wall-clock values throughout the app, with no timezone handling anywhere
else either; worth revisiting together if Render's server timezone and UK
local time ever drift apart, e.g. across a BST/GMT change on a UTC server).

    python scripts/check_shift_notifications.py
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.db import get_connection
from app.notification_settings import notify_admins

GRACE_MINUTES = 15
# A shift older than this is treated as stale, not "missed" - without a cap,
# the very first run after switching this on would trawl through every
# never-clocked-in shift in the venue's whole history and fire a flood of
# alerts for weeks-old shifts nobody cares about any more.
LOOKBACK_HOURS = 24


def _already_considered(conn, shift_id, notification_type):
    return conn.execute(
        "SELECT 1 FROM shift_notification_log WHERE shift_id = ? AND notification_type = ?",
        (shift_id, notification_type),
    ).fetchone() is not None


def _mark_considered(conn, shift_id, notification_type):
    # Marked regardless of whether the notification type is actually
    # enabled for the venue right now — otherwise, turning it on later
    # would retroactively flood admins with alerts for every shift that
    # went unclocked while it was off. Once a shift's been considered for
    # a given check, it's done, even if nothing was actually sent that time.
    conn.execute(
        "INSERT OR IGNORE INTO shift_notification_log (shift_id, notification_type) VALUES (?, ?)",
        (shift_id, notification_type),
    )


def check_missed_clock_ins(conn, now: datetime) -> int:
    cutoff = (now - timedelta(minutes=GRACE_MINUTES)).strftime("%Y-%m-%d %H:%M")
    earliest = (now - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%d %H:%M")
    rows = conn.execute(
        """SELECT shift.id AS shift_id, shift.venue_id, shift.shift_date, shift.start_time,
                  person.name AS person_name
           FROM shift
           JOIN person ON person.id = shift.person_id
           LEFT JOIN attendance ON attendance.shift_id = shift.id
           WHERE shift.status = 'scheduled' AND shift.person_id IS NOT NULL
           AND attendance.clock_in_at IS NULL
           AND (shift.shift_date || ' ' || shift.start_time) <= ?
           AND (shift.shift_date || ' ' || shift.start_time) >= ?""",
        (cutoff, earliest),
    ).fetchall()

    sent = 0
    for row in rows:
        if _already_considered(conn, row["shift_id"], "missed_clock_in"):
            continue
        venue = conn.execute("SELECT * FROM venue WHERE id = ?", (row["venue_id"],)).fetchone()
        notify_admins(
            conn, venue, "missed_clock_in",
            f"Missed clock-in — {venue['name']}",
            f"{row['person_name']} hasn't clocked in for their {row['shift_date']} "
            f"{row['start_time']} shift at {venue['name']}.",
        )
        _mark_considered(conn, row["shift_id"], "missed_clock_in")
        sent += 1
    conn.commit()
    return sent


def check_missed_clock_outs(conn, now: datetime) -> int:
    cutoff = (now - timedelta(minutes=GRACE_MINUTES)).strftime("%Y-%m-%d %H:%M")
    earliest = (now - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%d %H:%M")
    rows = conn.execute(
        """SELECT shift.id AS shift_id, shift.venue_id, shift.shift_date, shift.end_time,
                  person.name AS person_name
           FROM shift
           JOIN person ON person.id = shift.person_id
           JOIN attendance ON attendance.shift_id = shift.id
           WHERE shift.status = 'scheduled' AND shift.person_id IS NOT NULL
           AND attendance.clock_in_at IS NOT NULL AND attendance.clock_out_at IS NULL
           AND (shift.shift_date || ' ' || shift.end_time) <= ?
           AND (shift.shift_date || ' ' || shift.end_time) >= ?""",
        (cutoff, earliest),
    ).fetchall()

    sent = 0
    for row in rows:
        if _already_considered(conn, row["shift_id"], "missed_clock_out"):
            continue
        venue = conn.execute("SELECT * FROM venue WHERE id = ?", (row["venue_id"],)).fetchone()
        notify_admins(
            conn, venue, "missed_clock_out",
            f"Missed clock-out — {venue['name']}",
            f"{row['person_name']} hasn't clocked out for their {row['shift_date']} "
            f"{row['end_time']} shift at {venue['name']}.",
        )
        _mark_considered(conn, row["shift_id"], "missed_clock_out")
        sent += 1
    conn.commit()
    return sent


if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        conn = get_connection()
        now = datetime.now()
        missed_in = check_missed_clock_ins(conn, now)
        missed_out = check_missed_clock_outs(conn, now)
        conn.close()
        print(f"Checked shifts: {missed_in} missed clock-in notice(s), {missed_out} missed clock-out notice(s) considered.")
