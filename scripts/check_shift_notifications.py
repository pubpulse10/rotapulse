"""
Local/manual runner for the missed-clock-in/missed-clock-out checks (the
actual logic lives in app/notification_settings.py so it can also run via
POST /internal/run-shift-notifications against the web service's live,
disk-backed database — that's what the Render Cron Job calls, since a Cron
Job has no persistent disk of its own to read a local SQLite file from).

Uses app.uk_time.uk_now() rather than the server's own local clock — real
report, 2026-08-19: this WAS "assumes the server's local clock matches the
venue's local time", exactly the BST/GMT drift this docstring used to warn
about revisiting, and it turned out to matter (a staff member's clock-in
time was recorded an hour behind UK time during BST). See app/uk_time.py
for the full explanation.

    python scripts/check_shift_notifications.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.db import get_connection
from app.notification_settings import check_missed_clock_ins, check_missed_clock_outs, remind_staff_to_clock_in
from app.uk_time import uk_now

if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        conn = get_connection()
        now = uk_now()
        missed_in = check_missed_clock_ins(conn, now)
        missed_out = check_missed_clock_outs(conn, now)
        staff_reminders = remind_staff_to_clock_in(conn, now)
        conn.close()
        print(
            f"Checked shifts: {missed_in} missed clock-in notice(s), {missed_out} missed clock-out "
            f"notice(s) considered, {staff_reminders} staff clock-in reminder(s) sent."
        )
