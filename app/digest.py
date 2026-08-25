"""
Automated weekly cost-vs-turnover digest (spec §10) — sent via the shared
notifications pipe (app/notifications.py) so the insight reaches the
landlord passively, rather than requiring a dashboard visit. Logged to
weekly_digest_log to prevent double-sending for the same week (run this
from scripts/send_weekly_digest.py on a schedule, e.g. Monday morning).
"""

from datetime import date, timedelta

from app.costs import actual_cost, predicted_cost
from app.date_format import format_uk_date
from app.notifications import send_email
from app.uk_time import uk_today


def _monday_of(on_date: date) -> date:
    return on_date - timedelta(days=on_date.weekday())


def send_digest_for_venue(db, venue) -> bool:
    """Returns True if a digest was sent, False if already sent this week
    or there's no one to send it to."""
    last_week_start = _monday_of(uk_today()) - timedelta(days=7)
    last_week_end = last_week_start + timedelta(days=6)

    already_sent = db.execute(
        "SELECT 1 FROM weekly_digest_log WHERE venue_id = ? AND week_start_date = ?",
        (venue["id"], last_week_start.isoformat()),
    ).fetchone()
    if already_sent:
        return False

    settings = db.execute("SELECT * FROM venue_settings WHERE venue_id = ?", (venue["id"],)).fetchone()
    target_pct = settings["target_staff_cost_percent"] if settings else None

    predicted = predicted_cost(venue["id"], last_week_start.isoformat(), last_week_end.isoformat())
    actual = actual_cost(venue["id"], last_week_start.isoformat(), last_week_end.isoformat())
    turnover_row = db.execute(
        "SELECT * FROM weekly_turnover WHERE venue_id = ? AND week_start_date = ?",
        (venue["id"], last_week_start.isoformat()),
    ).fetchone()
    actual_turnover = (turnover_row["actual_amount"] or turnover_row["predicted_amount"] or 0) if turnover_row else 0

    lines = [
        f"RotaPulse weekly digest: {venue['name']}",
        f"Week: {format_uk_date(last_week_start)} to {format_uk_date(last_week_end)}",
        f"Staff cost — predicted: £{predicted:.2f}, actual: £{actual:.2f}",
    ]
    if actual_turnover:
        actual_pct = round(actual / actual_turnover * 100, 1) if actual_turnover else None
        lines.append(f"Turnover: £{actual_turnover:.2f} — staff cost was {actual_pct}% of turnover")
        if target_pct:
            status = "on track" if actual_pct <= target_pct else "over target"
            lines.append(f"Target: {target_pct}% — {status}")

    owner = db.execute(
        """SELECT person.email FROM person
           JOIN venue_membership ON venue_membership.person_id = person.id
           JOIN app_access ON app_access.venue_membership_id = venue_membership.id
           WHERE venue_membership.venue_id = ? AND app_access.permission_level = 'app_admin'
           AND person.pub_id IS NOT NULL AND person.email IS NOT NULL LIMIT 1""",
        (venue["id"],),
    ).fetchone()
    if owner and owner["email"]:
        send_email(owner["email"], f"Your RotaPulse weekly digest — {venue['name']}", "\n".join(lines))

    db.execute(
        "INSERT INTO weekly_digest_log (venue_id, week_start_date) VALUES (?, ?)",
        (venue["id"], last_week_start.isoformat()),
    )
    db.commit()
    return True
