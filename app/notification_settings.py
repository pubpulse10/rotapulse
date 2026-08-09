"""
Owner-configurable admin notifications. Five fixed event types, each with
its own enabled/disabled flag, delivery method, and chosen recipients
(any app_admin/rota_admin at the venue) — the owner decides who hears about
what, not a fixed "notify everyone" rule.

Two triggers feed into the same notify_admins() helper:
- Event-triggered (swap_request, leave_request, open_shift_claimed): called
  directly from the staff-facing route that causes the event, right after
  it commits.
- Scheduled (missed_clock_in, missed_clock_out): app/notification_settings
  doesn't run these itself — scripts/check_shift_notifications.py does,
  intended to run every few minutes via an external scheduler (Render Cron
  Job), same "not auto-scheduled by the app" pattern as
  scripts/send_weekly_digest.py.
"""

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
