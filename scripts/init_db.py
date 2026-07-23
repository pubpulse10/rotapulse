"""
Creates the schema (non-destructive, CREATE TABLE IF NOT EXISTS +
_add_column_if_missing only) and, the first time it's run, seeds a single
**dev** venue so local development/testing doesn't need to go through the
real shared-PubPulse-account + Stripe provisioning flow:
  - slug "dev", pub_id 0 (a placeholder — no real PubPulse account owns this
    locally), an active rota_subscription so the trial/subscription gate
    never blocks local testing
  - one owner person (app_admin + rota_admin, no password — logs in via the
    dev pub_id session same as production would)
  - one venue_role ("Bar staff")

This is NOT how real venues get created in production — that's
app.venues.setup(), reached via the shared PubPulse account. This script is
dev/test convenience only. Never deletes or overwrites existing data — safe
to re-run any time.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_app_id, get_connection, init_schema

DEV_SLUG = "dev"
DEV_PUB_ID = 0


def seed(conn):
    venue = conn.execute("SELECT * FROM venue WHERE slug = ?", (DEV_SLUG,)).fetchone()
    if venue is None:
        cur = conn.execute(
            "INSERT INTO venue (pub_id, name, slug) VALUES (?, ?, ?)",
            (DEV_PUB_ID, "Dev Venue", DEV_SLUG),
        )
        venue_id = cur.lastrowid
        conn.execute("INSERT INTO venue_settings (venue_id, target_staff_cost_percent) VALUES (?, 28)", (venue_id,))
        conn.execute("INSERT INTO rota_subscription (venue_id, plan) VALUES (?, 'active')", (venue_id,))

        owner_cur = conn.execute("INSERT INTO person (name, pub_id) VALUES (?, ?)", ("Dev Owner", DEV_PUB_ID))
        person_id = owner_cur.lastrowid
        membership_cur = conn.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')",
            (person_id, venue_id),
        )
        membership_id = membership_cur.lastrowid
        app_id = get_app_id(conn, "rotapulse")
        for level in ("app_admin", "rota_admin"):
            conn.execute(
                """INSERT INTO app_access (venue_membership_id, app_id, permission_level, status, accepted_at, approved_at)
                   VALUES (?, ?, ?, 'active', datetime('now'), datetime('now'))""",
                (membership_id, app_id, level),
            )
        conn.execute("INSERT INTO venue_role (venue_id, name) VALUES (?, 'Bar staff')", (venue_id,))
        print(f"Created dev venue (id={venue_id}, slug={DEV_SLUG!r}).")
    else:
        print("Dev venue already exists — nothing to seed.")

    conn.commit()


if __name__ == "__main__":
    conn = get_connection()
    init_schema(conn)
    seed(conn)
    conn.close()
    print(f"Done. Local dev venue is at /v/{DEV_SLUG}/rota/ — simulate the shared session with pub_id={DEV_PUB_ID} to reach it as the owner.")
