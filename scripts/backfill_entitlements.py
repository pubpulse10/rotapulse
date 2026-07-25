"""
One-off: pushes every existing venue's current plan state to the PubPulse
Hub's /internal/entitlements — same purpose as the sibling apps' own
backfill_entitlements.py, needed once if venues already existed locally
before the Hub integration was wired up (e.g. after a fresh Hub deploy, or
after Hub data was reset).

Also enriches any existing subscription's renewal date (current_period_end)
before pushing — that field was added after some real subscriptions already
existed, so their local row has it as NULL until this script fetches it
from Stripe once. Tolerates a since-cancelled subscription (or any other
Stripe error) without aborting the whole run.

    python scripts/backfill_entitlements.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stripe

from app import config, create_app
from app.billing import _period_end_iso, _push_entitlement
from app.db import get_connection


def _enrich_period_end(conn, venue_id, sub):
    if not sub or not sub["stripe_subscription_id"] or sub["current_period_end"]:
        return
    try:
        stripe_sub = stripe.Subscription.retrieve(sub["stripe_subscription_id"])
        period_end = _period_end_iso(stripe_sub)
    except stripe.error.StripeError as e:
        print(f"venue_id={venue_id}: couldn't enrich renewal date — {e}")
        return
    if period_end:
        conn.execute(
            "UPDATE rota_subscription SET current_period_end = ? WHERE venue_id = ?",
            (period_end, venue_id),
        )
        conn.commit()


if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        stripe.api_key = config.STRIPE_SECRET_KEY
        conn = get_connection()
        venues = conn.execute("SELECT id FROM venue WHERE pub_id IS NOT NULL").fetchall()
        for venue in venues:
            sub = conn.execute(
                "SELECT * FROM rota_subscription WHERE venue_id = ?", (venue["id"],)
            ).fetchone()
            _enrich_period_end(conn, venue["id"], sub)
            _push_entitlement(conn, venue["id"])
        conn.close()
        print(f"Pushed entitlements for {len(venues)} venue(s).")
