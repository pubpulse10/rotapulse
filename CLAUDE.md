# CLAUDE.md — RotaPulse (PubPulse app family)

RotaPulse is the staff rota, attendance and labour-cost app in the PubPulse family. `README.md` has the stack,
local setup and architecture notes; this file holds the invariants and dated decisions that have to survive
between sessions. It was started on 10 September 2026 for the Rewardful decision below. Add to it rather than
re-deciding something already recorded here.

## Invariants — do not break these

- **`create_app()` calls `db.init_schema()`, and must keep doing so.** That call is the only thing that makes
  a migration reach the live database. Every migration lives in `init_schema()` (`CREATE TABLE IF NOT EXISTS`
  + `_add_column_if_missing`), it is additive-only and idempotent, and nothing else in the running app runs
  it — not `wsgi.py`, not the Dockerfile, not `docker-entrypoint.sh`. Before 16 September 2026 it was absent,
  so three steps of leave work shipped to production with the code present and the columns missing.
  `/health` cannot catch that: it reports `RENDER_GIT_COMMIT`, an environment variable, so it confirms the
  deploy while the schema stays behind. `tests/test_schema_bootstrap.py` is the tripwire. TaskPulse still has
  this gap; pubpulse-hub and pricepulse do not.

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

### 2026-09-21 — The approval step has to announce itself, at both ends

Support report from The Queens Head: "the account seems to get created but then cannot log in."
Nothing was broken. Staff finish their invite, land in `pending_approval`, and an admin has to
approve them before `rota_auth` grants any permission level. Neither side was told:

- The staff member typed the **right** password, `rota_login` let them through, and
  `require_permission` redirected them straight back to the login form with no flash at all —
  indistinguishable from a wrong password. `login()` now checks `access_statuses()` before
  creating the session and explains (`inactive_access_message()`, shared with
  `require_permission`'s redirect so a session that goes stale mid-visit says the same thing).
  A wrong password still gets the old vague message — never confirm account state to someone
  who hasn't proved who they are.
- The admin was told nothing whatsoever. Now: a count badge on the Staff nav link (context
  processor `_inject_pending_staff_approvals`, admins only, failure-swallowed like `whoami`),
  the same count on the Staff page, and an email to the venue's admins the moment someone
  finishes their invite (`onboarding._tell_admins_someone_is_waiting`).

**That email deliberately does NOT go through `notification_settings.notify_admins()`.** That
system is silent unless the venue has already enabled the event type and picked recipients,
which a venue setting itself up for the first time has not — so routing it there would have
sent nothing to the very people who hit this. Like the invite itself, it has to arrive by
default. Same reasoning as `remind_staff_to_clock_in`.

Still open, for the owner: whether an approval step earns its keep at all when the landlord
invited the person themselves.

### 2026-09-17 — A job role is archived, never deleted once it has been used

`delete_role` used to 500. `venue_membership.job_role_id`, `shift.venue_role_id` and
`leave_block_role.venue_role_id` all reference `venue_role(id)`, the connection runs with
`PRAGMA foreign_keys = ON`, and only the first and third had a friendly refusal in front of them.
Deleting a role that had ever appeared on a rota raised `sqlite3.IntegrityError: FOREIGN KEY
constraint failed`, and the landlord got an error page rather than an explanation. Old shifts are
never tidied up, so after a few weeks of use that is every role.

The owner's decision, 17 September 2026, was **archive — neither a plain refusal nor nulling the
shifts out**:

- **`venue_role.archived_at`** — NULL means in use. `app/roles.py` is the only place that knows the
  rule, and every screen that offers a role goes through its `active_roles()`.
- **Delete now only ever removes a role nothing has touched.** The roles screen works out per row
  which action can succeed and shows Delete or Archive accordingly, rather than a button that
  refuses for nearly everything on the page.
- **Nulling `shift.venue_role_id` out was considered and rejected.** On a future OPEN shift the
  role is what narrows `notify_open_shift` to the people who can work it, so a nulled-out kitchen
  shift would quietly text the whole venue — the same shape of trap as a blocked date left with no
  roles, which applies to everyone.
- **An archived role stays in a `<select>` that already holds it** (`active_roles(include_ids=…)`),
  labelled "(archived)". Leave it out and the browser posts nothing for it, so saving an unrelated
  field on that form silently clears the role.
- `create_role` **restores** an archived role of the same name (NOCASE) instead of tripping
  `UNIQUE(venue_id, name)`, and `rename_role` refuses a clash with a message. Both were 500s before,
  and they matter more now: the role in the way can be archived, and so not on the screen to explain
  itself.
- Role ids submitted for a **blocked date are still validated against ALL roles**, archived included.
  That check exists to reject another pub's role id; narrowing it to active roles would silently drop
  an archived one and leave the block with no roles at all — which applies to everyone.

The only free-standing "delete" left is for a role nobody ever used. `tests/test_admin_roles.py`
covers all of it.

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
