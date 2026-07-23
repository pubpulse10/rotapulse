"""
Revokes app_access rows still sitting at status='invited' past their own
invite_expires_at — tidies up the staff list so an admin doesn't see
long-dead invites as if they were still live. Intended to run on a
schedule, same deployment-step pattern as TaskPulse's scripts/purge.py
(not auto-scheduled by the app itself).

    python scripts/purge_stale_invites.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_connection

if __name__ == "__main__":
    conn = get_connection()
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE app_access SET status = 'revoked' WHERE status = 'invited' AND invite_expires_at < ?",
        (now_iso,),
    )
    conn.commit()
    conn.close()
    print(f"Revoked {cur.rowcount} stale invite(s).")
