# RotaPulse

Staff rota, attendance, and labour-cost tracking for pub landlords — the third app in the PubPulse family
(alongside PricePulse and TaskPulse). Built from `Businesses\PubPulse\RotaPulse\RotaPulse — Product Specification.docx`.

## Stack

Flask + raw `sqlite3` (no ORM) + Jinja2 + Flask-WTF + waitress, mirroring TaskPulse's proven pattern: an app-factory
(`app/__init__.py`), blueprint-per-concern, `flask.g`-cached DB connections, additive-only schema migrations
(`CREATE TABLE IF NOT EXISTS` + `_add_column_if_missing`).

## Local setup

```bash
python -m venv .venv
./.venv/Scripts/pip install -r requirements.txt
cp .env.example .env   # then fill in SECRET_KEY at minimum
./.venv/Scripts/python scripts/init_db.py   # creates schema + a seeded "dev" venue (slug=dev, pub_id=0)
./.venv/Scripts/python run.py               # dev server on http://localhost:5053
```

Local dev has no real shared-PubPulse login to test the owner path through a browser — the dev venue's owner is
recognised via `session['pub_id'] == 0`, which only a real PricePulse login (or a hand-crafted session cookie) can
set. `scripts/init_db.py`'s own output reminds you of this. Staff/rota_admin accounts created via the normal invite
flow (`/v/dev/admin/staff` once logged in as the owner) use RotaPulse's own local password login instead, and are
fully testable in a browser once invited.

Run icon generation once (or after the source logo changes):
```bash
./.venv/Scripts/python scripts/generate_icons.py
```

## Tests

```bash
./.venv/Scripts/python -m pytest tests/ -q
```

## Architecture notes (see the build plan / commit history for full detail)

- **Two login paths**: the venue owner (`app_admin` + `rota_admin`) is recognised purely via the shared PubPulse
  session cookie (`session['pub_id']`), auto-provisioned on first visit (`app/venues.py`) — no password, ever, for
  this path. Invited staff and any delegated `rota_admin` who isn't the owner use RotaPulse's own local
  password login (`app/rota_login.py`), since they have no PricePulse account.
- **Shared "PubPulse People" layer** (`pub_company`/`venue`/`person`/`venue_membership`/`app`/`app_access`) lives in
  RotaPulse's own database — there's no real shared identity service in the family yet, so this follows the same
  unenforced `pub_id`-convention TaskPulse's own `venues.pub_id` already uses.
- **Billing**: a single Stripe Price (`billing_scheme="tiered"`, `tiers_mode="volume"`) with four flat-fee staff-count
  bands (`app/billing.py`). Quantity = count of active, billable staff (an admin-only account never counts).
  `scripts/create_stripe_price.py` is the one-off setup script for the Product/Price.
- **TaskPulse cross-app integration**: `app/internal.py` exposes `POST /internal/clock-status` (bearer-authed,
  shared `INTERNAL_API_SECRET`) so TaskPulse's own staff-selection screen can auto-recognise a person already
  clocked in via RotaPulse — see `../taskpulse/app/rotapulse_client.py` and `../taskpulse/app/staff.py::select_name()`.
- **Sensitive data** (home address, phone, face photo — spec §3): a standalone consent checkbox at onboarding,
  avatar/attendance-photo files served through an authenticated route (`app/media.py`), not plain `/static/`, and an
  `app_admin`-only erasure action (`app/consent.py`) that clears identity fields but keeps anonymised hours/pay
  figures for payroll retention once a staff member has left.

## Not yet done

- **Deployment.** This build is local-only per the original build plan — no Render service, no custom domain (the
  account's 2 custom-domain slots are already used by PricePulse + TaskPulse; a plan upgrade is needed first), no
  live Stripe products, no real Twilio account.
- **pubpulse-hub's `app/main.py`** still shows RotaPulse as "coming soon" (`active: False`) — the auto-mode safety
  classifier blocked flipping that specific edit during the build session. `app/config.py`'s `ROTAPULSE_URL` var is
  already added; flipping the `APPS` list entry in `main.py` to `active: True, url: config.ROTAPULSE_URL` is a
  two-line follow-up.
- **Shared `tokens.css` brand-kit** — still deferred, per the family's own earlier decision to revisit once RotaPulse
  started. RotaPulse's `app/static/style.css` reuses the same navy/green "Pulse" palette as the sibling apps by eye,
  not from a shared file.
