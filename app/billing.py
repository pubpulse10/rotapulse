"""
Venue subscription: trial + live Stripe Checkout + webhook. Directly mirrors
taskpulse/app/billing.py's proven pattern (itself mirroring pubpulse's), but
with genuine staff-count tiered pricing (spec §9.1) instead of a flat
binary active/inactive plan.

Tiering approach: ONE Stripe Price with billing_scheme="tiered",
tiers_mode="volume" — volume mode charges the entire quantity at whichever
single tier's flat_amount it falls into, which is exactly "1-5 staff = £10
flat" rather than a per-seat multiplication. Quantity = count of active,
billable ROTA_STAFF_DETAIL rows (admin-only accounts never count — spec
§2.1/§9.1). A venue above the top tier's max still pays the top tier's
price rather than being blocked (user decision).

Tier changes call stripe.Subscription.modify(..., proration_behavior=
"always_invoice") — this is what makes proration "immediate" per spec
§9.1, rather than rolling it into the next regular invoice.

Known regression class to avoid (already bit both sibling apps once): Stripe
SDK event objects from this SDK version don't support dict-style .get() —
only attribute access / [] indexing. Every field read off event.data.object
below uses getattr(obj, "field", None), never .get(). Tests must mock with
SimpleNamespace, never a plain dict, or this can pass silently again.
"""

from datetime import date, datetime, timezone

import requests
import stripe
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

from app import config
from app.db import get_db, get_rota_subscription
from app.notifications import send_email
from app.rota_auth import register_identity, require_permission
from app.venue_scope import register_venue_scope

stripe.api_key = config.STRIPE_SECRET_KEY

billing_bp = Blueprint("billing", __name__, url_prefix="/v/<slug>/billing")
register_venue_scope(billing_bp)
register_identity(billing_bp)


def count_billable_staff(venue_id: int) -> int:
    """Active app_access on the rotapulse app, with an accompanying
    rota_staff_detail row — admin-only accounts (no pay record) never
    count, matching spec §2.1/§9.1 exactly."""
    db = get_db()
    row = db.execute(
        """SELECT COUNT(DISTINCT venue_membership.id) AS n
           FROM venue_membership
           JOIN app_access ON app_access.venue_membership_id = venue_membership.id
           JOIN rota_staff_detail ON rota_staff_detail.venue_membership_id = venue_membership.id
           WHERE venue_membership.venue_id = ? AND venue_membership.status = 'active'
           AND app_access.status = 'active'
           AND app_access.app_id = (SELECT id FROM app WHERE key = 'rotapulse')""",
        (venue_id,),
    ).fetchone()
    return row["n"] if row else 0


def tier_for_count(count: int) -> int:
    """1-4, per spec §9.1's four bands. A count above the top band's max
    still returns the top tier (capped, not blocked)."""
    for index, (max_staff, _price_pence) in enumerate(config.STAFF_TIERS, start=1):
        if count <= max_staff:
            return index
    return len(config.STAFF_TIERS)


def _period_end_iso(stripe_subscription_obj) -> str | None:
    """Converts a Stripe subscription object's current_period_end (a Unix
    timestamp) into the same ISO-date-string convention every other date
    column in this family already uses. Mirrors the sibling apps' own
    helper exactly."""
    ts = getattr(stripe_subscription_obj, "current_period_end", None)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def _push_entitlement(db, venue_id):
    """Notifies the PubPulse Hub of this venue's plan state, keyed by the
    venue's pub_id — mirrors the sibling apps' own push exactly. Best-effort
    and silent on failure."""
    if not config.INTERNAL_API_SECRET:
        return
    venue = db.execute("SELECT pub_id FROM venue WHERE id = ?", (venue_id,)).fetchone()
    sub = get_rota_subscription(db, venue_id)
    if venue is None or venue["pub_id"] is None or sub is None:
        return
    try:
        requests.post(
            f"{config.PUBPULSE_HUB_URL}/internal/entitlements",
            json={
                "pub_id": int(venue["pub_id"]), "app_key": "rotapulse",
                "plan": sub["plan"], "trial_ends_at": sub["trial_ends_at"],
                "subscription_status": sub["subscription_status"],
                "renewal_at": sub["current_period_end"],
                "stripe_customer_id": sub["stripe_customer_id"],
                "stripe_subscription_id": sub["stripe_subscription_id"],
            },
            headers={"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"},
            timeout=5,
        )
    except requests.RequestException:
        pass


def current_venue_plan(venue_id: int) -> str:
    db = get_db()
    row = get_rota_subscription(db, venue_id)
    if row is None:
        return "inactive"
    if row["plan"] == "active":
        return "active"
    if row["trial_ends_at"] and row["trial_ends_at"] >= date.today().isoformat():
        return "active"
    return "inactive"


def sync_stripe_quantity(venue_id: int) -> None:
    """Called whenever a staff-count-relevant state change happens (a
    membership approved-active, or set to 'left') — pushes the new billable
    headcount to Stripe as the subscription item's quantity, with immediate
    proration. No-ops if there's no live Stripe subscription yet (still on
    trial, or Stripe isn't configured)."""
    db = get_db()
    sub = get_rota_subscription(db, venue_id)
    if not sub or not sub["stripe_subscription_id"] or not sub["stripe_subscription_item_id"]:
        return
    new_count = count_billable_staff(venue_id)
    stripe.Subscription.modify(
        sub["stripe_subscription_id"],
        items=[{"id": sub["stripe_subscription_item_id"], "quantity": max(new_count, 1)}],
        proration_behavior="always_invoice",
    )
    db.execute(
        "UPDATE rota_subscription SET current_tier = ? WHERE venue_id = ?",
        (tier_for_count(new_count), venue_id),
    )
    db.commit()


@billing_bp.route("/upgrade", methods=["POST"])
def upgrade():
    venue = g.venue
    if not config.STRIPE_PRICE_STAFF_TIERED:
        abort(400)

    db = get_db()
    existing = get_rota_subscription(db, venue["id"])
    quantity = max(count_billable_staff(venue["id"]), 1)

    session_kwargs = {
        "mode": "subscription",
        "line_items": [{"price": config.STRIPE_PRICE_STAFF_TIERED, "quantity": quantity}],
        "success_url": url_for("billing.success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": url_for("billing.cancelled", _external=True),
        "client_reference_id": str(venue["id"]),
    }
    if existing and existing["stripe_customer_id"]:
        session_kwargs["customer"] = existing["stripe_customer_id"]
    else:
        landlord_email = session.get("landlord_email")
        if landlord_email:
            session_kwargs["customer_email"] = landlord_email

    checkout_session = stripe.checkout.Session.create(**session_kwargs)
    return redirect(checkout_session.url, code=303)


@billing_bp.route("/success")
def success():
    return render_template("billing/success.html", venue=g.venue)


@billing_bp.route("/cancelled")
def cancelled():
    return render_template("billing/cancelled.html", venue=g.venue)


@billing_bp.route("/subscription")
@require_permission("app_admin")
def subscription():
    venue = g.venue
    sub = get_rota_subscription(get_db(), venue["id"])
    staff_count = count_billable_staff(venue["id"])
    return render_template(
        "billing/subscription.html",
        venue=venue,
        sub=sub,
        plan=current_venue_plan(venue["id"]),
        staff_count=staff_count,
        tier=tier_for_count(staff_count),
        tiers=config.STAFF_TIERS,
        stripe_configured=bool(config.STRIPE_PRICE_STAFF_TIERED),
    )


@billing_bp.route("/manage", methods=["POST"])
@require_permission("app_admin")
def manage():
    venue = g.venue
    sub = get_rota_subscription(get_db(), venue["id"])
    if not sub or not sub["stripe_customer_id"]:
        flash("There's no subscription to manage yet — subscribe below first.", "error")
        return redirect(url_for("billing.subscription"))
    portal_session = stripe.billing_portal.Session.create(
        customer=sub["stripe_customer_id"],
        return_url=url_for("billing.subscription", _external=True),
    )
    return redirect(portal_session.url, code=303)


def register_webhook(app):
    """Registered directly on the app, not on billing_bp — Stripe calls one
    fixed URL, no /v/<slug> prefix. CSRF-exempt (see app/__init__.py) — no
    session, no CSRF token, authenticity comes from the signature."""

    @app.route("/billing/webhook", methods=["POST"])
    def stripe_webhook():
        payload = request.get_data()
        sig_header = request.headers.get("Stripe-Signature", "")
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, config.STRIPE_WEBHOOK_SECRET)
        except (ValueError, stripe.error.SignatureVerificationError):
            abort(400)

        db = get_db()
        if event["type"] == "checkout.session.completed":
            checkout = event["data"]["object"]
            venue_id = getattr(checkout, "client_reference_id", None)
            if venue_id:
                stripe_subscription_id = getattr(checkout, "subscription", None)
                subscription_item_id = None
                period_end = None
                if stripe_subscription_id:
                    stripe_sub = stripe.Subscription.retrieve(stripe_subscription_id)
                    items = getattr(stripe_sub, "items", None)
                    data = getattr(items, "data", None) if items else None
                    if data:
                        subscription_item_id = getattr(data[0], "id", None)
                    # Same retrieve call already made above for
                    # subscription_item_id — no extra API call needed to
                    # also grab the renewal date off the same object.
                    period_end = _period_end_iso(stripe_sub)

                db.execute(
                    """UPDATE rota_subscription
                       SET plan = 'active', stripe_customer_id = ?, stripe_subscription_id = ?,
                           stripe_subscription_item_id = ?, subscription_status = 'active',
                           current_period_end = ?
                       WHERE venue_id = ?""",
                    (getattr(checkout, "customer", None), stripe_subscription_id, subscription_item_id,
                     period_end, venue_id),
                )
                db.commit()
                _push_entitlement(db, venue_id)

                venue = db.execute("SELECT name FROM venue WHERE id = ?", (venue_id,)).fetchone()
                venue_name = venue["name"] if venue else "your venue"
                customer_details = getattr(checkout, "customer_details", None)
                payer_email = getattr(customer_details, "email", None) or getattr(checkout, "customer_email", None)
                if payer_email:
                    send_email(
                        payer_email,
                        "Your RotaPulse subscription is confirmed",
                        f"Thanks — {venue_name} is now on a paid RotaPulse subscription.\n\n"
                        "You can manage your plan, payment details and invoices any time from "
                        "your subscription page in RotaPulse.",
                    )
                if config.SUBSCRIBER_NOTIFY_EMAIL:
                    send_email(
                        config.SUBSCRIBER_NOTIFY_EMAIL,
                        f"New RotaPulse subscription: {venue_name}",
                        f"Venue: {venue_name}\nPayer email: {payer_email or '(not available)'}\nPlan: active (paid)",
                    )

        elif event["type"] in ("customer.subscription.updated", "customer.subscription.deleted"):
            sub = event["data"]["object"]
            status = getattr(sub, "status", None)
            active = status in ("active", "trialing")
            # current_period_end is already directly on this event object —
            # no extra Stripe API call needed here, unlike checkout.session.completed.
            period_end = _period_end_iso(sub)
            existing = db.execute(
                "SELECT venue_id FROM rota_subscription WHERE stripe_subscription_id = ?", (sub.id,)
            ).fetchone()
            db.execute(
                """UPDATE rota_subscription SET plan = ?, subscription_status = ?, current_period_end = ?
                   WHERE stripe_subscription_id = ?""",
                ("active" if active else "inactive", status, period_end, sub.id),
            )
            db.commit()
            if existing is not None:
                _push_entitlement(db, existing["venue_id"])

        return "", 200

    return stripe_webhook
