"""
Owner-configurable admin notifications. Five fixed event types, each with
its own enabled/disabled flag, delivery method, and chosen recipients
(any app_admin/rota_admin at the venue) — the owner decides who hears about
what, not a fixed "notify everyone" rule.

Two triggers feed into the same notify_admins() helper:
- Event-triggered (swap_request, leave_request, open_shift_claimed): called
  directly from the staff-facing route that causes the event, right after
  it commits.
- Scheduled (missed_clock_in, missed_clock_out): check_missed_clock_ins/outs
  below, run periodically. They live here (not just in scripts/) so they can
  run against the web service's own live, disk-backed DB connection via the
  /internal/run-shift-notifications endpoint (app/internal.py) — a Render
  Cron Job has no persistent disk of its own, so it calls that endpoint over
  HTTP instead of opening the database directly. scripts/check_shift_notifications.py
  still exists as a thin wrapper for local/manual runs.
"""

from datetime import datetime, timedelta

from app.notifications import send_email, send_sms

NOTIFICATION_TYPES = [
    ("missed_clock_in", "Missed clock-in (15 min after shift start)"),
    ("missed_clock_out", "Missed clock-out (15 min after shift end)"),
    ("swap_request", "Swap request submitted"),
    ("leave_request", "Leave request submitted"),
    ("open_shift_claimed", "Open shift claimed"),
]
NOTIFICATION_TYPE_KEYS = {key for key, _label in NOTIFICATION_TYPES}
NOTIFICATION_TYPE_LABELS = dict(NOTIFICATION_TYPES)

METHODS = ["email", "sms", "both"]


def notify_admins(db, venue, notification_type: str, subject: str, body: str) -> None:
    """No-op if this notification type isn't enabled for the venue, or has
    no recipients selected — silent by design, this isn't an error case."""
    setting = db.execute(
        "SELECT * FROM notification_setting WHERE venue_id = ? AND notification_type = ?",
        (venue["id"], notification_type),
    ).fetchone()
    if setting is None or not setting["enabled"]:
        return

    recipients = db.execute(
        """SELECT person.email, person.mobile FROM notification_recipient
           JOIN person ON person.id = notification_recipient.person_id
           WHERE notification_recipient.notification_setting_id = ?""",
        (setting["id"],),
    ).fetchall()

    for r in recipients:
        if setting["method"] in ("email", "both") and r["email"]:
            send_email(r["email"], subject, body)
        if setting["method"] in ("sms", "both") and r["mobile"]:
            send_sms(r["mobile"], body)


GRACE_MINUTES = 15
# A shift older than this is treated as stale, not "missed" - without a cap,
# the very first run after switching this on would trawl through every
# never-clocked-in shift in the venue's whole history and fire a flood of
# alerts for weeks-old shifts nobody cares about any more.
LOOKBACK_HOURS = 24


def _already_considered(db, shift_id, notification_type):
    return db.execute(
        "SELECT 1 FROM shift_notification_log WHERE shift_id = ? AND notification_type = ?",
        (shift_id, notification_type),
    ).fetchone() is not None


def _mark_considered(db, shift_id, notification_type):
    # Marked regardless of whether the notification type is actually
    # enabled for the venue right now — otherwise, turning it on later
    # would retroactively flood admins with alerts for every shift that
    # went unclocked while it was off. Once a shift's been considered for
    # a given check, it's done, even if nothing was actually sent that time.
    db.execute(
        "INSERT OR IGNORE INTO shift_notification_log (shift_id, notification_type) VALUES (?, ?)",
        (shift_id, notification_type),
    )


def check_missed_clock_ins(db, now: datetime) -> int:
    cutoff = (now - timedelta(minutes=GRACE_MINUTES)).strftime("%Y-%m-%d %H:%M")
    earliest = (now - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%d %H:%M")
    rows = db.execute(
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
        if _already_considered(db, row["shift_id"], "missed_clock_in"):
            continue
        venue = db.execute("SELECT * FROM venue WHERE id = ?", (row["venue_id"],)).fetchone()
        notify_admins(
            db, venue, "missed_clock_in",
            f"Missed clock-in — {venue['name']}",
            f"{row['person_name']} hasn't clocked in for their {row['shift_date']} "
            f"{row['start_time']} shift at {venue['name']}.",
        )
        _mark_considered(db, row["shift_id"], "missed_clock_in")
        sent += 1
    db.commit()
    return sent


def check_missed_clock_outs(db, now: datetime) -> int:
    cutoff = (now - timedelta(minutes=GRACE_MINUTES)).strftime("%Y-%m-%d %H:%M")
    earliest = (now - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%d %H:%M")
    rows = db.execute(
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
        if _already_considered(db, row["shift_id"], "missed_clock_out"):
            continue
        venue = db.execute("SELECT * FROM venue WHERE id = ?", (row["venue_id"],)).fetchone()
        notify_admins(
            db, venue, "missed_clock_out",
            f"Missed clock-out — {venue['name']}",
            f"{row['person_name']} hasn't clocked out for their {row['shift_date']} "
            f"{row['end_time']} shift at {venue['name']}.",
        )
        _mark_considered(db, row["shift_id"], "missed_clock_out")
        sent += 1
    db.commit()
    return sent
