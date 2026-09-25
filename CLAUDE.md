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

- **`app_admin` belongs to the venue owner, and nothing outside `venues.setup()` may create or
  destroy it.** It is not a grant somebody made: it follows from owning the PubPulse account,
  and `setup()` writes `app_admin` + `rota_admin` once. `app/internal.py`'s Hub push must never
  write it, never delete it, and must ignore a push aimed at the owner's own person row
  entirely (their `person.pub_id` is set; a staff person's never is). `rota_auth` rebuilds the
  pair whenever a session that resolved to the owner by `pub_id` turns out to be missing
  `app_admin`, so a venue that has already lost it repairs itself on the next page view.
  Losing it is nearly silent — rota_admin still runs the whole day-to-day app, so all the owner
  sees is Settings gone from the menu and the pay-rate field gone from a staff record
  (25 September 2026).

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

### 2026-09-22 — Help & feedback: the widget is the Hub's, the identity is ours

RotaPulse is the first app to carry the family's shared "Help & feedback" widget. Two
pieces, and the split matters:

- **The widget itself is served by the Hub** (`{{ pubpulse_hub_url }}/static/help-widget.js`,
  loaded in `base.html` for a signed-in person outside a support session). **Do not vendor a
  copy into `app/static/`.** One form across five apps is the entire point; four copies is
  four forms that drift until one of them is still sending a type the Hub stopped accepting.
- **`app/help_feedback.py` is ours, and its only job is saying who is submitting.** The
  widget posts here — on our own origin, with our own CSRF token — and this forwards it to
  the Hub with `INTERNAL_API_SECRET` (pubpulse-hub `docs/decisions.md` D3). It never posts
  to the Hub from the browser: that would mean a credentialed CORS surface on the identity
  hub and a Hub CSRF token fetched into our page.

**The browser never says who it is.** `pub_id`, `user_email`, `user_name` and `app` are all
set server-side from `g.person` / `g.venue` / the session, overwriting anything posted. A
`pub_id` trusted from a form would let any signed-in customer file feedback as any other —
and the Hub's "Your conversations" page keys off exactly those values, so it would be a way
to read a stranger's support replies too. An invited staff member has `person.pub_id = NULL`,
hence the fall back to `venue.pub_id` and then the shared cookie.

**No `register_venue_gate` on that blueprint, deliberately** — the same exemption the
billing blueprint has. That gate locks a whole venue when the subscription is not active,
and someone who has just been locked out is exactly the person who needs to ask why.

We validate nothing about the message and store nothing: lengths, enums and the screenshot
are the Hub's rules, checked in the Hub, and the screenshot is streamed straight through
rather than saved (a copy here would be a second store of customer screenshots with none of
the Hub's magic-byte checks or re-encoding done to it). The Hub's JSON and status code are
passed back untouched, because the widget shows `errors[0]` to the customer.

`app.pubpulse.co.uk` is now in the report-only CSP's `script-src` — added now rather than
when the policy is promoted, so promoting it cannot silently stop the widget loading.

The recipe, for when the other three apps follow: pubpulse-hub
`docs/help-widget-integration.md`. Tests: `tests/test_help_feedback.py` (14).

### 2026-09-25 — A pay rate that couldn't be edited was the owner losing app_admin

Reported after the first payroll run: a staff member's hourly rate had been entered wrongly
at onboarding and the Staff → Edit screen offered no way to correct it. The field was not
missing. It is `app_admin`-only (spec §2.1/§4) and has been since it was built, with a test;
what had gone was the owner's `app_admin`.

`app/internal.py::_link_or_create_person` matched an incoming Hub person to a local person
row **by email**, which exists for a good reason — RotaPulse's own invite flow is still how
staff are actually onboarded ([[family-access-plan]] Phase 4 is deferred), so the same human
can arrive down both paths and must not end up as two records. But the owner's person row
carries the landlord's account email, so a Hub person invited under that address adopted it,
and the next line —

    DELETE FROM app_access WHERE venue_membership_id = ? AND app_id = ?

— then took `app_admin` and `rota_admin` with it and put back a single row at whatever level
the Hub held. Everything else went on working, which is why it read as a missing field rather
than as lost access.

Three changes, and deliberately all three rather than only the one that stops it recurring:

1. The email match skips any row with a `pub_id` (that is the owner, by definition).
2. A push that still resolves to an owner's row — because an earlier version already stamped
   the Hub id on it — returns `{"ok": true, "skipped": "venue owner"}` without touching the
   person or their access. Ignoring it is right: the owner already holds more than any grant
   could give, and the alternative is renaming the landlord and demoting them.
3. `rota_auth._restore_owner_access()` puts the pair back for anyone the `pub_id` cookie
   already proves is the owner. A one-off repair script would have needed a Render Shell and
   would fix exactly one venue; this fixes the one that was broken, on the next page view,
   and any other that ever gets there.

Also, the Edit screen now **shows** a rota_admin the pay rate as read-only text saying only
the account owner can change it, instead of leaving the field out altogether. An absent field
reads as "this app can't do that" rather than "you can't" — which is a fair part of why a
wrong rate survived a payroll run. And `edit_staff` no longer treats an absent
`hourly_pay_rate` as 0: the rota_admin form has no such field, and `or 0` on a missing value
would wipe a rate rather than keep it.

Payroll reads the current rate live, so correcting the rate and re-running the report for the
period is the whole fix — there is no frozen historical figure to back-correct.

`tests/test_owner_app_admin_survives.py`.

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
