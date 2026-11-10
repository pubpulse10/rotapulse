"""
The Staff Suite bundle: 25% off BOTH RotaPulse and HR-Pulse while a pub has
both. FAMILY COPY -- byte-identical in rotapulse/app/staff_suite.py and
hrpulse/app/staff_suite.py; change one, change both.

Who decides: the PubPulse Hub. It sees every app's subscription state (each
app pushes /internal/entitlements), so whenever RotaPulse's or HR-Pulse's
entitlement changes it works out whether the pub has both and calls
POST /internal/staff-suite {"pub_id", "on"} on BOTH apps. The Hub's own Stripe
key is read-only by design, so each app changes its own subscription here.

What it does: puts one indefinite 25% coupon (STRIPE_BUNDLE_COUPON_ID) on the
subscription, or takes it off. Any OTHER discount already on the subscription
is kept -- Subscription.modify(discounts=[...]) replaces the whole list, so the
existing ones are passed back. Idempotent: if the coupon is already in the
state asked for, Stripe isn't touched. The change shows from the next invoice.
"""

import logging

import stripe

logger = logging.getLogger(__name__)


def _coupon_of(discount):
    """The coupon id behind a Discount. Newer Stripe API versions moved it
    from discount.coupon to discount.source.coupon; read either."""
    source = getattr(discount, "source", None)
    coupon = getattr(source, "coupon", None) if source is not None else None
    if coupon is None:
        coupon = getattr(discount, "coupon", None)
    return coupon if isinstance(coupon, str) or coupon is None else getattr(coupon, "id", None)


def set_discount(subscription_id, coupon_id, on: bool):
    """Returns True if Stripe was changed, False if it already matched, and
    None if it couldn't be done (logged; the caller reports it)."""
    if not subscription_id or not coupon_id:
        return None
    try:
        sub = stripe.Subscription.retrieve(subscription_id, expand=["discounts"])
        discounts = list(getattr(sub, "discounts", None) or [])
        ours = [d for d in discounts if not isinstance(d, str) and _coupon_of(d) == coupon_id]
        if bool(ours) == on:
            return False
        keep = [{"discount": d if isinstance(d, str) else d.id}
                for d in discounts if isinstance(d, str) or _coupon_of(d) != coupon_id]
        if on:
            keep.append({"coupon": coupon_id})
        stripe.Subscription.modify(subscription_id, discounts=keep or "")
        return True
    except stripe.error.StripeError as exc:
        logger.warning("Couldn't %s the Staff Suite discount on %s: %s",
                       "add" if on else "remove", subscription_id, exc)
        return None
