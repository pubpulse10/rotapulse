"""
Sends the automated weekly cost-vs-turnover digest (spec §10) to every
active venue. Intended to run on a schedule (e.g. Monday morning) via
Windows Task Scheduler / Render Cron — not auto-scheduled by the app
itself, same deployment-step pattern as TaskPulse's scripts/purge.py.

    python scripts/send_weekly_digest.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.db import get_connection
from app.digest import send_digest_for_venue

if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        conn = get_connection()
        venues = conn.execute(
            "SELECT * FROM venue WHERE id IN (SELECT venue_id FROM rota_subscription WHERE plan = 'active')"
        ).fetchall()
        sent = 0
        for venue in venues:
            if send_digest_for_venue(conn, venue):
                sent += 1
        conn.close()
        print(f"Sent {sent} of {len(venues)} venue digest(s).")
