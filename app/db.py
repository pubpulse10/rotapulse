"""
SQLite schema for RotaPulse.

Follows TaskPulse's proven idiom exactly: CREATE TABLE IF NOT EXISTS
throughout, plus `_add_column_if_missing` for any column added to a table
that already existed in an earlier deployment (SQLite has no ADD COLUMN IF
NOT EXISTS). Running this module against any existing database, fresh or
already-populated, is always safe and never destructive.

Architectural note on the "shared PubPulse People/Venue/App-Access layer"
(spec §2.1): pub_company/venue/person/venue_membership/app/app_access/
company_admin all live HERE, in RotaPulse's own database — there is no real
shared identity service anywhere in the family today (PricePulse, TaskPulse
and the Hub each own a separate SQLite file). They're "shared" only in the
same unenforced pub_id-convention TaskPulse's own venues.pub_id already
uses, never a real cross-database foreign key. See person.pub_id below.
"""

import sqlite3
from pathlib import Path

import flask

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "rotapulse.db"

SCHEMA = """
-- ---------- Identity / venue layer (spec §2.1) ----------

CREATE TABLE IF NOT EXISTS pub_company (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS venue (
    id INTEGER PRIMARY KEY,
    pub_company_id INTEGER REFERENCES pub_company(id),
    pub_id INTEGER,              -- convention copy of PricePulse's pubs.id —
                                  -- no real cross-database FK is possible,
                                  -- same idiom as taskpulse's venues.pub_id
    name TEXT NOT NULL,
    postcode TEXT,
    latitude REAL,                -- geocoded from postcode at setup (see
    longitude REAL,                -- app/geocoding.py) — used for the
                                    -- clock-in/out distance check (spec §6.1)
    slug TEXT,                    -- URL-safe, unique — see idx_venue_slug below
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS venue_settings (
    venue_id INTEGER PRIMARY KEY REFERENCES venue(id),
    pay_period_type TEXT NOT NULL DEFAULT 'weekly',   -- weekly | every_n_weeks | monthly
    pay_period_interval_weeks INTEGER,
    pay_period_anchor_date TEXT,
    pay_period_month_end_day INTEGER,
    pay_day_offset INTEGER,          -- label-only, per spec §7.1
    holiday_year_start_date TEXT,    -- MM-DD, landlord-defined (spec §8)
    target_staff_cost_percent REAL
);

CREATE TABLE IF NOT EXISTS rota_subscription (
    venue_id INTEGER PRIMARY KEY REFERENCES venue(id),
    plan TEXT NOT NULL DEFAULT 'inactive',   -- 'inactive' | 'active'
    trial_ends_at TEXT,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    stripe_subscription_item_id TEXT,        -- needed for quantity-only Subscription.modify
    subscription_status TEXT,
    current_tier INTEGER                     -- 1-4, cached for display only; recomputed
                                              -- from count_billable_staff(), not authoritative
);

CREATE TABLE IF NOT EXISTS person (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    mobile TEXT,
    avatar_url TEXT,
    date_of_birth TEXT,              -- optional; birthday/anniversary reminder (spec §10)
    password_hash TEXT,               -- NULL until onboarding stage 4 completes; stays
                                       -- NULL forever for a venue owner authenticated
                                       -- only via the shared pub SSO cookie
    pub_id INTEGER,                  -- set ONLY on the one auto-provisioned owner
                                       -- person per venue — anchor for resolving
                                       -- identity from the shared session (see
                                       -- app/rota_auth.py). Convention, not an
                                       -- enforced FK, same as venue.pub_id above.
    hub_person_id INTEGER,            -- Phase 2: the Hub's person.id for a staff
                                       -- member invited via the Hub (NULL for the
                                       -- owner and for bespoke-invited people).
    consent_given_at TEXT,            -- sensitive-data consent (spec §3, see app/consent.py)
    erased_at TEXT,                    -- right-to-erasure marker (see app/consent.py)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Deliberately no UNIQUE(email): two unrelated venues could invite genuinely
-- different humans who share an email address, or the same human could work
-- two unconnected pubs pre-multi-venue — forcing global uniqueness would be
-- wrong. Per-venue uniqueness is enforced at venue_membership instead.

CREATE TABLE IF NOT EXISTS venue_role (
    id INTEGER PRIMARY KEY,
    venue_id INTEGER NOT NULL REFERENCES venue(id),
    name TEXT NOT NULL,
    UNIQUE(venue_id, name)
);

CREATE TABLE IF NOT EXISTS venue_membership (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id),
    venue_id INTEGER NOT NULL REFERENCES venue(id),
    job_role_id INTEGER REFERENCES venue_role(id),
    status TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'left'
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(person_id, venue_id)
);

CREATE TABLE IF NOT EXISTS app_access (
    id INTEGER PRIMARY KEY,
    venue_membership_id INTEGER NOT NULL REFERENCES venue_membership(id),
    app_id INTEGER NOT NULL REFERENCES app(id),
    permission_level TEXT NOT NULL,           -- 'app_admin' | 'rota_admin' | 'staff'
    status TEXT NOT NULL DEFAULT 'invited',   -- invited | pending_approval | active | revoked
    invite_method TEXT,                        -- 'email' | 'sms'
    invite_token_hash TEXT,
    invite_expires_at TEXT,
    invited_at TEXT,
    accepted_at TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(venue_membership_id, app_id, permission_level)
);

CREATE TABLE IF NOT EXISTS company_admin (       -- present, unused until multi-venue (spec §2.2)
    person_id INTEGER NOT NULL REFERENCES person(id),
    pub_company_id INTEGER NOT NULL REFERENCES pub_company(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (person_id, pub_company_id)
);

-- ---------- RotaPulse extension data (spec §2.3) ----------

CREATE TABLE IF NOT EXISTS rota_staff_detail (
    venue_membership_id INTEGER PRIMARY KEY REFERENCES venue_membership(id),
    hourly_pay_rate NUMERIC NOT NULL DEFAULT 0,   -- admin-only, never shown on staff self-edit
    home_address TEXT,
    availability TEXT,              -- JSON: {"mon":true,"tue":false,...}
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Presence of a row here is what makes a membership count as billable
-- "staff" (spec §2.1/§9.1) — see app.billing.count_billable_staff().

CREATE TABLE IF NOT EXISTS rota_password_reset_token (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id),
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);

-- ---------- The rota (spec §5) ----------

CREATE TABLE IF NOT EXISTS shift (
    id INTEGER PRIMARY KEY,
    venue_id INTEGER NOT NULL REFERENCES venue(id),
    person_id INTEGER REFERENCES person(id),   -- NULL when status='open' (spec §5.4).
                                                 -- References PERSON directly, not
                                                 -- venue_membership — deliberate per
                                                 -- spec §2.2, groundwork for future
                                                 -- cross-venue cover.
    venue_role_id INTEGER REFERENCES venue_role(id),  -- role needed, esp. for open-shift matching
    shift_date TEXT NOT NULL,     -- YYYY-MM-DD
    start_time TEXT NOT NULL,     -- HH:MM
    end_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',   -- 'scheduled' | 'open'
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_shift_venue_date ON shift(venue_id, shift_date);

CREATE TABLE IF NOT EXISTS day_off_override (
    id INTEGER PRIMARY KEY,
    venue_id INTEGER NOT NULL REFERENCES venue(id),
    person_id INTEGER NOT NULL REFERENCES person(id),
    override_date TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(venue_id, person_id, override_date)
);

CREATE TABLE IF NOT EXISTS leave_request (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id),
    venue_id INTEGER NOT NULL,    -- scoping convenience (spec says "hangs off PERSON";
                                  -- no multi-venue in V1 so this is safe)
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | declined
    requested_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT,
    decided_by_person_id INTEGER REFERENCES person(id)
);

CREATE TABLE IF NOT EXISTS shift_swap_request (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL REFERENCES shift(id),
    from_person_id INTEGER NOT NULL REFERENCES person(id),
    to_person_id INTEGER NOT NULL REFERENCES person(id),
    status TEXT NOT NULL DEFAULT 'pending_peer',  -- pending_peer|pending_admin|approved|declined
    requested_at TEXT NOT NULL DEFAULT (datetime('now')),
    peer_responded_at TEXT,
    admin_decided_at TEXT,
    admin_decided_by_person_id INTEGER REFERENCES person(id)
);
-- No separate history table per spec §5.5 — this row itself is the lightweight record.

CREATE TABLE IF NOT EXISTS shift_open_notification (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL REFERENCES shift(id),
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_by_person_id INTEGER REFERENCES person(id),
    recipient_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS week_notification (
    venue_id INTEGER NOT NULL REFERENCES venue(id),
    week_start_date TEXT NOT NULL,
    notified_at TEXT NOT NULL DEFAULT (datetime('now')),
    notified_by_person_id INTEGER REFERENCES person(id),
    recipient_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (venue_id, week_start_date)
);
-- One row per (venue, week) once staff have been sent their shift times
-- for that week — the "unnotified -> notified" flip is this row simply
-- existing or not; its PRIMARY KEY is what makes a second notify attempt
-- for the same week rejectable with a plain existence check, no separate
-- status column needed.

-- ---------- Attendance (spec §6) ----------

CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL UNIQUE REFERENCES shift(id),  -- 1:1 with shift
    clock_in_at TEXT,
    clock_in_lat REAL,
    clock_in_lng REAL,
    clock_in_location_confirmed INTEGER,   -- 0/1/NULL — NULL if geolocation declined
    clock_out_at TEXT,
    clock_out_lat REAL,
    clock_out_lng REAL,
    clock_out_location_confirmed INTEGER,
    photo_url TEXT,
    variance_flag INTEGER NOT NULL DEFAULT 0
);

-- ---------- Dashboard / differentiators (spec §10-11) ----------

CREATE TABLE IF NOT EXISTS event_tag (
    id INTEGER PRIMARY KEY,
    venue_id INTEGER NOT NULL REFERENCES venue(id),
    tag_date TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(venue_id, tag_date, label)
);

CREATE TABLE IF NOT EXISTS weekly_turnover (
    venue_id INTEGER NOT NULL REFERENCES venue(id),
    week_start_date TEXT NOT NULL,   -- always a Monday
    predicted_amount NUMERIC,
    actual_amount NUMERIC,       -- editable indefinitely, never locked (spec §11)
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (venue_id, week_start_date)
);
-- One turnover figure per week, not per day — day-level entry (the
-- original design) was more scrutiny than a small pub actually wants to
-- give this daily; predicted/actual staff cost still come from
-- SHIFT/ATTENDANCE at whatever date-range granularity is needed, this
-- table only holds the turnover half of the comparison.

CREATE TABLE IF NOT EXISTS weekly_digest_log (
    id INTEGER PRIMARY KEY,
    venue_id INTEGER NOT NULL REFERENCES venue(id),
    week_start_date TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(venue_id, week_start_date)
);

CREATE TABLE IF NOT EXISTS weather_cache (
    venue_id INTEGER NOT NULL REFERENCES venue(id),
    forecast_date TEXT NOT NULL,
    temperature_c REAL,
    weather_code INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (venue_id, forecast_date)
);

-- ---------- Owner-configurable admin notifications ----------
-- One row per (venue, notification_type) — see app/notification_settings.py
-- for the fixed list of types. Recipients are chosen per type, not global,
-- so e.g. missed-clock-in alerts can go to just the owner while swap
-- requests go to a delegated rota_admin (Steve's explicit design ask).

CREATE TABLE IF NOT EXISTS notification_setting (
    id INTEGER PRIMARY KEY,
    venue_id INTEGER NOT NULL REFERENCES venue(id),
    notification_type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    method TEXT NOT NULL DEFAULT 'email',   -- 'email' | 'sms' | 'both'
    UNIQUE(venue_id, notification_type)
);

CREATE TABLE IF NOT EXISTS notification_recipient (
    id INTEGER PRIMARY KEY,
    notification_setting_id INTEGER NOT NULL REFERENCES notification_setting(id),
    person_id INTEGER NOT NULL REFERENCES person(id),
    UNIQUE(notification_setting_id, person_id)
);

-- Idempotency log for the scheduled missed-clock-in/out checker (run
-- externally on a timer, see scripts/check_shift_notifications.py) — without
-- this, every run of the checker would re-notify for the same still-missed
-- clock-in/out every time it runs.
CREATE TABLE IF NOT EXISTS shift_notification_log (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL REFERENCES shift(id),
    notification_type TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(shift_id, notification_type)
);
"""


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers and a writer proceed concurrently (fewer "database is
    # locked" errors under Waitress' threads); busy_timeout waits up to 5s for
    # a lock instead of failing immediately.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _add_column_if_missing(conn, table, column, coltype):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_schema(conn=None):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_venue_slug ON venue(slug)")
        for key in ("pricepulse", "taskpulse", "rotapulse"):
            conn.execute("INSERT OR IGNORE INTO app (key) VALUES (?)", (key,))
        # Superseded by weekly_turnover (day-level turnover entry replaced
        # by week-level) — only ever drops the old table if it's genuinely
        # empty, never touches it if a deployment already has real rows in
        # it, consistent with never running destructive schema changes
        # against real data without asking first.
        old_table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='turnover_figure'"
        ).fetchone()
        if old_table_exists:
            row_count = conn.execute("SELECT COUNT(*) AS n FROM turnover_figure").fetchone()["n"]
            if row_count == 0:
                conn.execute("DROP TABLE turnover_figure")

        # Real Stripe renewal date, for the PubPulse Hub's Owner Console —
        # added after rota_subscription already existed in production.
        _add_column_if_missing(conn, "rota_subscription", "current_period_end", "TEXT")
        _add_column_if_missing(conn, "person", "hub_person_id", "INTEGER")
        # 'sent' | 'failed' | NULL (not yet attempted — pre-existing rows
        # from before this column existed). Set whenever an invite/resend
        # actually attempts delivery, so a silent failure (e.g. the SMS
        # E.164-format bug) is visible on the Staff page instead of only in
        # server logs the admin has no access to.
        _add_column_if_missing(conn, "app_access", "invite_delivery_status", "TEXT")
        # Employment start date — was missing entirely, despite the
        # "birthday/anniversary differentiator" (spec §10) already having a
        # dedicated dashboard page whose title promised anniversaries it had
        # no data to ever compute. ISO date, ADD-side of the same person's
        # rota_staff_detail row (not person, since a start date is specific
        # to working at THIS venue, unlike date_of_birth).
        _add_column_if_missing(conn, "rota_staff_detail", "start_date", "TEXT")
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def get_db():
    if "db" not in flask.g:
        flask.g.db = get_connection()
    return flask.g.db


def close_db(_exception=None):
    conn = flask.g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)


def get_venue_by_slug(conn, slug):
    return conn.execute("SELECT * FROM venue WHERE slug = ?", (slug,)).fetchone()


def get_all_venues(conn):
    """Every venue, for the family-admin support picker (app/family_admin.py)
    — the only place that needs a cross-venue listing rather than one
    resolved from a URL slug."""
    return conn.execute("SELECT * FROM venue ORDER BY name").fetchall()


def get_rota_subscription(conn, venue_id):
    return conn.execute("SELECT * FROM rota_subscription WHERE venue_id = ?", (venue_id,)).fetchone()


def get_app_id(conn, key):
    row = conn.execute("SELECT id FROM app WHERE key = ?", (key,)).fetchone()
    return row["id"] if row else None
