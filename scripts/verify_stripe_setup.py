"""
Read-only audit of the live Stripe configuration for RotaPulse's 4-tier
staff-count pricing (see app/billing.py and app/config.py::STAFF_TIERS).
Makes no changes — only GETs the Price and webhook endpoint list and
compares them against what config.py expects.

Run wherever the real STRIPE_SECRET_KEY actually lives (e.g. Render's Shell
for this service, not a local .env) — this repo's local .env deliberately
has no Stripe keys in it.

    python scripts/verify_stripe_setup.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stripe

from app import config

stripe.api_key = config.STRIPE_SECRET_KEY


def _pounds(pence: int) -> str:
    return f"£{pence / 100:.2f}"


def main() -> None:
    print("== Environment ==")
    if not config.STRIPE_SECRET_KEY:
        raise SystemExit("STRIPE_SECRET_KEY is not set in this environment. Stop.")
    mode = "LIVE" if config.STRIPE_SECRET_KEY.startswith("sk_live_") else "TEST"
    print(f"Secret key mode: {mode}")
    print(f"Publishable key set: {bool(config.STRIPE_PUBLISHABLE_KEY)}")
    print(f"Webhook signing secret set: {bool(config.STRIPE_WEBHOOK_SECRET)}")
    print(f"STRIPE_PRICE_STAFF_TIERED: {config.STRIPE_PRICE_STAFF_TIERED or '(not set)'}")

    if not config.STRIPE_PRICE_STAFF_TIERED:
        raise SystemExit("\nSTRIPE_PRICE_STAFF_TIERED is not set — subscribing is not wired up. Stop.")

    print("\n== Price object ==")
    price = stripe.Price.retrieve(config.STRIPE_PRICE_STAFF_TIERED, expand=["tiers"])
    print(f"id: {price.id}")
    print(f"active: {price.active}")
    print(f"currency: {price.currency}")
    print(f"billing_scheme: {price.billing_scheme}")
    print(f"tiers_mode: {price.tiers_mode}")
    recurring = getattr(price, "recurring", None)
    print(f"recurring interval: {getattr(recurring, 'interval', None)}")

    print("\n== Tiers on the live Price ==")
    live_tiers = []
    for t in price.tiers or []:
        up_to = t.up_to if t.up_to is not None else "inf"
        print(f"  up_to={up_to}  flat_amount={t.flat_amount} ({_pounds(t.flat_amount) if t.flat_amount else '?'})")
        if t.up_to is not None:
            live_tiers.append((t.up_to, t.flat_amount))

    print("\n== Expected tiers (app/config.py::STAFF_TIERS) ==")
    for max_staff, price_pence in config.STAFF_TIERS:
        print(f"  up_to={max_staff}  flat_amount={price_pence} ({_pounds(price_pence)})")

    print("\n== Comparison ==")
    ok = True
    if price.billing_scheme != "tiered" or price.tiers_mode != "volume":
        print("MISMATCH: billing_scheme/tiers_mode is not tiered+volume as expected.")
        ok = False
    if live_tiers != config.STAFF_TIERS:
        print("MISMATCH: live tiers don't match config.STAFF_TIERS exactly.")
        ok = False
    if not price.active:
        print("MISMATCH: this Price is not active in Stripe.")
        ok = False
    if ok:
        print("Live Stripe Price matches config.STAFF_TIERS exactly — 4 tiers, tiered/volume, active.")

    print("\n== Registered webhook endpoints ==")
    endpoints = stripe.WebhookEndpoint.list(limit=20)
    if not endpoints.data:
        print("No webhook endpoints registered at all in this Stripe account/mode.")
    for ep in endpoints.data:
        flag = " <-- this app's /billing/webhook" if ep.url.rstrip("/").endswith("/billing/webhook") else ""
        print(f"  {ep.url}  status={ep.status}  events={len(ep.enabled_events)}{flag}")


if __name__ == "__main__":
    main()
