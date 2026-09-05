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

remind_staff_to_clock_in() is a separate, simpler thing entirely: a direct
SMS to the staff member who's actually late, not an admin notification. It
runs on the same schedule but is NOT part of the owner-configurable
enabled/method/recipients system above — always on, no off switch, per a
direct 2026-08-14 request.
"""

from datetime import datetime, timedelta

from app.notifications import send_email, send_sms

NOTIFICATION_TYPES = [
    ("missed_clock_in", "Missed clock-in (15 min after shift start)"),
    ("missed_clock_out", "Missed clock-out (15 min after shift end)"),
    ("swap_request", "Swap request submitted"),
    ("leave_request", "Leave request submitted"),
    ("open_shift_claimed", "Open shift claimed"),
    ("ad_hoc_shift", "Unplanned shift or early start needs approval"),
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


STAFF_REMINDER_GRACE_MINUTES = 10


def remind_staff_to_clock_in(db, now: datetime) -> int:
    """An SMS nudge direct to the staff member themselves, requested
    2026-08-14 alongside making actual clock-in/out times visible to
    admins (staff being paid for actual time worked was the stated
    motivation for both). Deliberately distinct from check_missed_clock_ins
    just below: that one tells ADMINS, is owner-configurable via
    notification_setting, and fires later at GRACE_MINUTES. This one is a
    simple, always-on reminder to the person who's actually late — not
    tied to the owner-configurable system, which is specifically about who
    among the admin tier hears about problems, not about staff-facing
    reminders. Uses its own shift_notification_log entry type so it runs
    independently of (and doesn't get skipped by) the admin-facing check
    for the same shift."""
    cutoff = (now - timedelta(minutes=STAFF_REMINDER_GRACE_MINUTES)).strftime("%Y-%m-%d %H:%M")
    earliest = (now - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%d %H:%M")
    rows = db.execute(
        """SELECT shift.id AS shift_id, shift.venue_id, shift.start_time,
                  person.name AS person_name, person.mobile AS person_mobile
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
        if _already_considered(db, row["shift_id"], "staff_clock_in_reminder"):
            continue
        _mark_considered(db, row["shift_id"], "staff_clock_in_reminder")
        if row["person_mobile"]:
            venue = db.execute("SELECT name FROM venue WHERE id = ?", (row["venue_id"],)).fetchone()
            delivered = send_sms(
                row["person_mobile"],
                f"Hi {row['person_name']}, you're due in for your {row['start_time']} shift at "
                f"{venue['name']} — don't forget to clock in when you arrive.",
            )
            if delivered:
                sent += 1
    db.commit()
    return sent


def check_missed_clock_outs(db, now: datetime) -> int:
    cutoff = (now - timedelta(minutes=GRACE_MINUTES)).strftime("%Y-%m-%d %H:%M")
    earliest = (now - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%d %H:%M")
    # A shift that ends earlier in the clock than it starts ran past midnight,
    # so it finishes on the FOLLOWING day. Pasting shift_date onto end_time
    # without that check put a 20:00-02:00 late shift's end 24 hours early,
    # which landed it inside the window while the person was still behind the
    # bar — an admin got "hasn't clocked out" mid-shift, and by the time they
    # really had finished, _already_considered had marked it done and the
    # genuine alert never came.
    #
    # Only end_time needs this. The two queries above key off start_time, and
    # a shift always starts on its own shift_date.
    #
    # Strictly less-than, not <=: equal times are the zero-length placeholder
    # an ad-hoc shift is created with, not a 24-hour shift. Same rule as
    # costs.shift_hours().
    ends_at = (
        "CASE WHEN shift.end_time < shift.start_time "
        "THEN date(shift.shift_date, '+1 day') || ' ' || shift.end_time "
        "ELSE shift.shift_date || ' ' || shift.end_time END"
    )
    rows = db.execute(
        f"""SELECT * FROM (
               SELECT shift.id AS shift_id, shift.venue_id, shift.shift_date,
                      shift.end_time, person.name AS person_name,
                      {ends_at} AS ends_at
               FROM shift
               JOIN person ON person.id = shift.person_id
               JOIN attendance ON attendance.shift_id = shift.id
               WHERE shift.status = 'scheduled' AND shift.person_id IS NOT NULL
               AND attendance.clock_in_at IS NOT NULL AND attendance.clock_out_at IS NULL
           )
           WHERE ends_at <= ? AND ends_at >= ?""",
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
