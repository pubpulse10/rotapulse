"""
Local/manual runner for the missed-clock-in/missed-clock-out checks (the
actual logic lives in app/notification_settings.py so it can also run via
POST /internal/run-shift-notifications against the web service's live,
disk-backed database — that's what the Render Cron Job calls, since a Cron
Job has no persistent disk of its own to read a local SQLite file from).

Assumes the server's local clock matches the venue's local time (no
timezone conversion — shift_date/start_time/end_time are stored as plain
wall-clock values throughout the app, with no timezone handling anywhere
else either; worth revisiting together if Render's server timezone and UK
local time ever drift apart, e.g. across a BST/GMT change on a UTC server).

    python scripts/check_shift_notifications.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.db import get_connection
from app.notification_settings import check_missed_clock_ins, check_missed_clock_outs

if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        conn = get_connection()
        now = datetime.now()
        missed_in = check_missed_clock_ins(conn, now)
        missed_out = check_missed_clock_outs(conn, now)
        conn.close()
        print(f"Checked shifts: {missed_in} missed clock-in notice(s), {missed_out} missed clock-out notice(s) considered.")
