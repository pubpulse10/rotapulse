"""
One-off setup script: creates the RotaPulse Stripe Product and the single
tiered/volume Price described in app/billing.py's module docstring. Run
once per Stripe environment (test mode, then live mode), then copy the
printed Price ID into STRIPE_PRICE_STAFF_TIERED.

    python scripts/create_stripe_price.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stripe

from app import config

stripe.api_key = config.STRIPE_SECRET_KEY

if __name__ == "__main__":
    if not config.STRIPE_SECRET_KEY:
        raise SystemExit("Set STRIPE_SECRET_KEY first (in .env or the environment).")

    product = stripe.Product.create(name="RotaPulse subscription")
    price = stripe.Price.create(
        product=product.id,
        currency="gbp",
        recurring={"interval": "month"},
        billing_scheme="tiered",
        tiers_mode="volume",
        tiers=[
            {"up_to": max_staff, "flat_amount": price_pence}
            for max_staff, price_pence in config.STAFF_TIERS
        ] + [{"up_to": "inf", "flat_amount": config.STAFF_TIERS[-1][1]}],
    )
    print(f"Product: {product.id}")
    print(f"Price:   {price.id}")
    print("Set STRIPE_PRICE_STAFF_TIERED to the Price ID above.")
