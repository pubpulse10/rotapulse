"""
One-off: pushes every existing venue's current plan state to the PubPulse
Hub's /internal/entitlements — same purpose as the sibling apps' own
backfill_entitlements.py, needed once if venues already existed locally
before the Hub integration was wired up (e.g. after a fresh Hub deploy, or
after Hub data was reset).

    python scripts/backfill_entitlements.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.billing import _push_entitlement
from app.db import get_connection

if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        conn = get_connection()
        venues = conn.execute("SELECT id FROM venue WHERE pub_id IS NOT NULL").fetchall()
        for venue in venues:
            _push_entitlement(conn, venue["id"])
        conn.close()
        print(f"Pushed entitlements for {len(venues)} venue(s).")
