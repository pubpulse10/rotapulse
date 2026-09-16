# CLAUDE.md — RotaPulse (PubPulse app family)

RotaPulse is the staff rota, attendance and labour-cost app in the PubPulse family. `README.md` has the stack,
local setup and architecture notes; this file holds the invariants and dated decisions that have to survive
between sessions. It was started on 10 September 2026 for the Rewardful decision below. Add to it rather than
re-deciding something already recorded here.

## Invariants — do not break these

- **Affiliate attribution (Rewardful) rides on Stripe Customer `metadata.referral`, never on
  `client_reference_id`.** Here `client_reference_id` carries the venue id, and the webhook and the
  `/billing/success` reconcile both depend on it. The referral lives in PricePulse, the family's identity store.
  `billing.apply_referral_metadata` reads it from `GET {PRICEPULSE_INTERNAL_URL}/internal/pubs/<pub_id>/referral`,
  keyed by the venue's `pub_id` (never the venue id), and writes the affiliate's **link token**, falling back to
  the referral id only if no token was captured. It tags a new Customer after Checkout (`_activate_from_checkout`)
  and a returning one BEFORE `checkout.Session.create`, because a returning customer gets no trial and is charged
  at once. It never overwrites a `referral` already on the Customer, and doesn't warn when that differs from ours:
  Rewardful always rewrites it to its own UUID. It never raises, so a failed lookup or Stripe call is logged and
  activation carries on. Read Stripe objects with attribute access, never `.get()`. In tests, `tests/conftest.py`
  stubs the PricePulse lookup for every test so none can reach the real one.

## Decisions

### 2026-09-16 — Leave types, allowances and blocked dates: BUILT

The full design is in **`docs/leave-design.md`** — read it before touching leave. Agreed with
the owner 15-16 September 2026 and built in three steps, all of them shipped. Headlines, so
this file is enough to avoid a wrong turn:

- **Five leave types, of which only paid leave reduces a balance.** Lieu is recorded but has
  NO earned ledger, so there is no lieu balance to show.
- **Allowance is `full_time_allowance x (usual days per week / full_time days per week)`**,
  rounded UP to the next half day, never below the statutory minimum, editable, and NOT a flat
  28 for everyone — statutory holiday is 5.6 WEEKS, so a flat 28 gives a two-day-a-week cleaner
  roughly two and a half times their entitlement. A NULL `allowance_days` means "work it out";
  a number means somebody typed it and it is never recalculated over.
- **Days and hours are frozen at approval** (`leave.freeze_counts`) so editing an availability
  cannot rewrite last year's history. Every record stores hours as well as days, so the 12.07%
  irregular-hours model can be added later without a migration.
- **Carry-over is entered by hand, never automatic**, one row per person per holiday year.
- **Paid leave reaches the payroll report** in days and hours but is deliberately NOT added to
  gross pay: RotaPulse does not calculate holiday pay, because for variable hours that is a
  52-week average and it belongs with payroll.
- **Blocked dates** (`leave_block` + `leave_block_role`) stop STAFF requesting leave, never the
  admin, and never apply to sick or maternity. A block with no roles applies to everyone; a
  block with roles does not catch somebody with no role set.

The availability bug is fixed: `leave.working_pattern()` returns None when availability was
never set, instead of the old default-to-worked that counted a week's leave as 7 days. Anything
that cannot be counted is now NAMED on screen rather than shown as nought.


### 2026-09-10 — Affiliate attribution via Rewardful

**Decision (family-wide text, same in every repo):** PubPulse pays affiliates through Rewardful, which attributes a
customer by `metadata.referral` on the Stripe Customer, one referral per Customer. The referral is captured once,
at registration (`data-rewardful` form → `referral` / `affiliate_id` / `affiliate_token` → PricePulse
`/internal/register` → `pub_settings.referral_*`). Every family app tags the Stripe Customer it creates or reuses
for the pub with the affiliate's **link token** (the referral id only as a fallback), reading the values from
PricePulse `/internal/pubs/<id>/referral`: after Checkout completes for a new Customer, before the Session for a
returning one. A token makes Rewardful create a fresh referral per Customer. A referral id reused on a second
Customer is silently ignored (tested live on 10 September 2026). The tag is idempotent and never overwrites an
existing one. **Never use `client_reference_id` for Rewardful**: it carries the venue id here (`pub_id` in
PricePulse) and the webhooks depend on it. A failed tag is logged, never fatal. Commission rules (rate, window,
pending period) live in Rewardful, not in code.

**How RotaPulse does it:**
- `app/billing.py`: `apply_referral_metadata(pub_id, customer_id)`, with the one HTTP call isolated in
  `_fetch_referral` (5-second timeout, the existing `INTERNAL_API_SECRET` bearer). No secret, no customer or no
  `pub_id` (including the dev venue's `pub_id = 0`) means it returns before any call. An unreferred pub gets no
  Stripe call at all.
- Checkout here is keyed by venue, so both call sites resolve the venue's `pub_id` (the family account id, which
  isn't unique per venue) and pass that. A venue without a `pub_id` is skipped.
- Call sites: `_activate_from_checkout`, after its commit and the Hub push (shared by the webhook and the return
  page); and `upgrade()`, in the branch that reuses a stored Customer, before `checkout.Session.create`.
- Nothing else changed: every Checkout Session kwarg and the webhook's `metadata.pubpulse_app` filter are as they
  were. `PRICEPULSE_INTERNAL_URL` defaults to the live PricePulse, so the deploy needs no new env var.
- **Known gap, accepted:** a returning venue whose Stripe Customer was deleted in the Dashboard
  (`_forget_deleted_customer` cleared the id) gets a brand-new Customer from Checkout with no trial. Its first
  invoice is therefore paid before the tag can land and needs "Generate commission" by hand in Rewardful. This is
  rare, and not worth pre-creating Customers for.

**Tests:** 25 in `tests/test_referrals.py`, plus the autouse `no_real_pricepulse_lookup` stub in
`tests/conftest.py`.
