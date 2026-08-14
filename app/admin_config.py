"""
Venue configuration, role management, staff directory/onboarding-invite/
approval, and sensitive-data erasure — all app_admin (venue config, roles,
erasure) or app_admin+rota_admin (day-to-day staff directory) per spec
§2.1/§5.2.
"""

import hashlib
import json
import secrets
from datetime import date, datetime, timedelta, timezone

import flask

from app import config
from app.billing import enforce_band
from app.consent import erase_person_sensitive_data
from app.db import get_app_id, get_db
from app.geocoding import geocode_postcode
from app.notification_settings import METHODS, NOTIFICATION_TYPES
from app.notifications import send_email, send_sms
from app.rota_auth import register_identity, require_permission
from app.venue_scope import register_venue_gate, register_venue_scope

admin_bp = flask.Blueprint("admin_config", __name__, url_prefix="/v/<slug>/admin")
register_venue_scope(admin_bp)
register_venue_gate(admin_bp)
register_identity(admin_bp)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


# ---------- Venue settings (app_admin only) ----------

MONTHS = [
    (1, "January"), (2, "February"), (3, "March"), (4, "April"),
    (5, "May"), (6, "June"), (7, "July"), (8, "August"),
    (9, "September"), (10, "October"), (11, "November"), (12, "December"),
]


def _parse_stored_holiday_start(value):
    """Stored internally as MM-DD (matches app/leave.py's parser) — this
    just recovers (day, month) ints to pre-select the settings form's two
    dropdowns. Defensive against any leftover malformed value from before
    the settings form validated its input, rather than crashing the
    settings page itself."""
    if not value:
        return None, None
    try:
        month_s, day_s = value.split("-")
        return int(day_s), int(month_s)
    except (ValueError, AttributeError):
        return None, None


@admin_bp.route("/settings", methods=["GET", "POST"])
@require_permission("app_admin")
def settings():
    db = get_db()
    venue_id = flask.g.venue["id"]
    if flask.request.method == "POST":
        form = flask.request.form

        venue_name = form.get("venue_name", "").strip()
        postcode = form.get("postcode", "").strip()
        if not venue_name:
            flask.flash("Venue name is required.", "error")
            return flask.redirect(flask.url_for("admin_config.settings"))

        # Day/month dropdowns, not free text — a previous free-text version
        # of this field let a malformed value ("0101" instead of "01-01")
        # get saved with no validation at all, which crashed every staff
        # member's leave page at that venue (app/leave.py's parser assumes
        # exactly MM-DD). Dropdowns make an ambiguous or malformed value
        # structurally impossible rather than needing to detect one.
        holiday_start_day = form.get("holiday_year_start_day", type=int)
        holiday_start_month = form.get("holiday_year_start_month", type=int)
        if holiday_start_day and holiday_start_month:
            try:
                date(2024, holiday_start_month, holiday_start_day)  # 2024 is a leap year, validates 29 Feb too
            except ValueError:
                flask.flash("That's not a real day and month.", "error")
                return flask.redirect(flask.url_for("admin_config.settings"))
            holiday_year_start_date = f"{holiday_start_month:02d}-{holiday_start_day:02d}"
        elif holiday_start_day or holiday_start_month:
            flask.flash("Choose both a day and a month for the holiday year start, or leave both blank.", "error")
            return flask.redirect(flask.url_for("admin_config.settings"))
        else:
            holiday_year_start_date = None

        current_postcode = flask.g.venue["postcode"] or ""
        if postcode and postcode.upper() != current_postcode.upper():
            # Only re-geocode when the postcode actually changed — this is
            # a network call (app/geocoding.py), no need to pay that cost
            # on every settings save.
            coords = geocode_postcode(postcode)
            if coords is None:
                flask.flash(
                    "Venue name saved, but that postcode couldn't be looked up — "
                    "clock-in location checks will use the previous coordinates until it's fixed.",
                    "error",
                )
                db.execute("UPDATE venue SET name = ?, postcode = ? WHERE id = ?", (venue_name, postcode, venue_id))
            else:
                db.execute(
                    "UPDATE venue SET name = ?, postcode = ?, latitude = ?, longitude = ? WHERE id = ?",
                    (venue_name, postcode, coords[0], coords[1], venue_id),
                )
        else:
            db.execute("UPDATE venue SET name = ?, postcode = ? WHERE id = ?", (venue_name, postcode or None, venue_id))

        db.execute(
            """UPDATE venue_settings SET pay_period_type = ?, pay_period_interval_weeks = ?,
               pay_period_anchor_date = ?, pay_period_month_end_day = ?, pay_day_offset = ?,
               holiday_year_start_date = ?, target_staff_cost_percent = ?
               WHERE venue_id = ?""",
            (
                form.get("pay_period_type", "weekly"),
                form.get("pay_period_interval_weeks", type=int),
                form.get("pay_period_anchor_date") or None,
                form.get("pay_period_month_end_day", type=int),
                form.get("pay_day_offset", type=int),
                holiday_year_start_date,
                form.get("target_staff_cost_percent", type=float),
                venue_id,
            ),
        )
        db.commit()
        flask.flash("Venue settings saved.")
        return flask.redirect(flask.url_for("admin_config.settings"))

    row = db.execute("SELECT * FROM venue_settings WHERE venue_id = ?", (venue_id,)).fetchone()
    current_day, current_month = _parse_stored_holiday_start(row["holiday_year_start_date"] if row else None)
    return flask.render_template(
        "admin/settings.html", venue=flask.g.venue, settings=row, MONTHS=MONTHS,
        holiday_year_start_day=current_day, holiday_year_start_month=current_month,
    )


# ---------- Roles (app_admin only, spec §5.2) ----------


@admin_bp.route("/roles")
@require_permission("app_admin")
def roles():
    db = get_db()
    role_rows = db.execute(
        "SELECT * FROM venue_role WHERE venue_id = ? ORDER BY name", (flask.g.venue["id"],)
    ).fetchall()
    return flask.render_template("admin/roles.html", roles=role_rows)


@admin_bp.route("/roles/create", methods=["POST"])
@require_permission("app_admin")
def create_role():
    db = get_db()
    name = flask.request.form.get("name", "").strip()
    if name:
        db.execute(
            "INSERT INTO venue_role (venue_id, name) VALUES (?, ?)", (flask.g.venue["id"], name)
        )
        db.commit()
    return flask.redirect(flask.url_for("admin_config.roles"))


@admin_bp.route("/roles/<int:role_id>/rename", methods=["POST"])
@require_permission("app_admin")
def rename_role(role_id):
    db = get_db()
    name = flask.request.form.get("name", "").strip()
    if name:
        db.execute(
            "UPDATE venue_role SET name = ? WHERE id = ? AND venue_id = ?",
            (name, role_id, flask.g.venue["id"]),
        )
        db.commit()
    return flask.redirect(flask.url_for("admin_config.roles"))


@admin_bp.route("/roles/<int:role_id>/delete", methods=["POST"])
@require_permission("app_admin")
def delete_role(role_id):
    db = get_db()
    venue_id = flask.g.venue["id"]
    in_use = db.execute(
        "SELECT COUNT(*) AS n FROM venue_membership WHERE job_role_id = ? AND venue_id = ?",
        (role_id, venue_id),
    ).fetchone()["n"]
    if in_use:
        flask.flash(
            f"Can't delete this role — {in_use} staff member(s) are still assigned to it. "
            "Reassign them to a different role first.",
            "error",
        )
        return flask.redirect(flask.url_for("admin_config.roles"))
    db.execute("DELETE FROM venue_role WHERE id = ? AND venue_id = ?", (role_id, venue_id))
    db.commit()
    flask.flash("Role deleted.")
    return flask.redirect(flask.url_for("admin_config.roles"))


# ---------- Admin notifications (app_admin only — the owner decides who
# hears about what, not each recipient for themselves) ----------


def _eligible_notification_recipients(db, venue_id):
    """Anyone with active app_admin or rota_admin access at this venue —
    the same pool the owner can pick from for any notification type."""
    return db.execute(
        """SELECT DISTINCT person.id, person.name, person.email, person.mobile
           FROM person
           JOIN venue_membership ON venue_membership.person_id = person.id
           JOIN app_access ON app_access.venue_membership_id = venue_membership.id
           WHERE venue_membership.venue_id = ? AND venue_membership.status = 'active'
           AND app_access.status = 'active'
           AND app_access.permission_level IN ('app_admin', 'rota_admin')
           AND app_access.app_id = (SELECT id FROM app WHERE key = 'rotapulse')
           ORDER BY person.name COLLATE NOCASE""",
        (venue_id,),
    ).fetchall()


@admin_bp.route("/notifications")
@require_permission("app_admin")
def notification_settings():
    db = get_db()
    venue_id = flask.g.venue["id"]
    settings_by_type = {
        row["notification_type"]: row
        for row in db.execute("SELECT * FROM notification_setting WHERE venue_id = ?", (venue_id,)).fetchall()
    }
    recipients_by_type = {}
    for key, _label in NOTIFICATION_TYPES:
        setting = settings_by_type.get(key)
        if setting is None:
            recipients_by_type[key] = set()
        else:
            rows = db.execute(
                "SELECT person_id FROM notification_recipient WHERE notification_setting_id = ?",
                (setting["id"],),
            ).fetchall()
            recipients_by_type[key] = {r["person_id"] for r in rows}

    return flask.render_template(
        "admin/notification_settings.html",
        notification_types=NOTIFICATION_TYPES,
        methods=METHODS,
        settings_by_type=settings_by_type,
        recipients_by_type=recipients_by_type,
        eligible_recipients=_eligible_notification_recipients(db, venue_id),
    )


@admin_bp.route("/notifications", methods=["POST"])
@require_permission("app_admin")
def save_notification_settings():
    db = get_db()
    venue_id = flask.g.venue["id"]
    form = flask.request.form
    eligible_ids = {p["id"] for p in _eligible_notification_recipients(db, venue_id)}

    for key, _label in NOTIFICATION_TYPES:
        enabled = 1 if form.get(f"enabled_{key}") else 0
        method = form.get(f"method_{key}", "email")
        if method not in METHODS:
            method = "email"
        # Only IDs actually eligible right now are honoured — a person who's
        # left or lost admin access since this list last loaded must not
        # stay a silent recipient just because their checkbox was posted.
        chosen_ids = {int(v) for v in form.getlist(f"recipients_{key}") if v.isdigit()} & eligible_ids

        db.execute(
            """INSERT INTO notification_setting (venue_id, notification_type, enabled, method)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(venue_id, notification_type) DO UPDATE SET
               enabled = excluded.enabled, method = excluded.method""",
            (venue_id, key, enabled, method),
        )
        # cur.lastrowid isn't reliable on the ON CONFLICT DO UPDATE path
        # (only reflects a genuine INSERT) — query it explicitly instead.
        setting_id = db.execute(
            "SELECT id FROM notification_setting WHERE venue_id = ? AND notification_type = ?",
            (venue_id, key),
        ).fetchone()["id"]

        db.execute("DELETE FROM notification_recipient WHERE notification_setting_id = ?", (setting_id,))
        for person_id in chosen_ids:
            db.execute(
                "INSERT INTO notification_recipient (notification_setting_id, person_id) VALUES (?, ?)",
                (setting_id, person_id),
            )

    db.commit()
    flask.flash("Notification settings saved.")
    return flask.redirect(flask.url_for("admin_config.notification_settings"))


# ---------- Staff directory / onboarding invite / approval ----------


def _display_status(membership_status, access_status):
    """Collapses the two underlying status fields — venue_membership.status
    (are they still a member of this venue at all: active/left) and
    app_access.status (their RotaPulse access stage: invited ->
    pending_approval -> active -> revoked) — into one label. Showing both
    raw values side by side (e.g. "active / active") reads as a meaningless
    duplicate for the common case where they happen to match; membership
    being 'left' is what actually matters once it's true, regardless of
    whatever access_status was last set to."""
    if membership_status == "left":
        return "Left"
    return {
        "invited": "Invited",
        "pending_approval": "Pending approval",
        "active": "Active",
        "revoked": "Revoked",
    }.get(access_status, access_status)


@admin_bp.route("/staff")
@require_permission("app_admin", "rota_admin")
def staff_list():
    db = get_db()
    venue_id = flask.g.venue["id"]
    rows = db.execute(
        """SELECT venue_membership.id AS membership_id, venue_membership.status AS membership_status,
                  person.id AS person_id, person.name, person.email, person.mobile,
                  person.avatar_url, person.date_of_birth,
                  venue_role.name AS role_name,
                  rota_staff_detail.hourly_pay_rate,
                  app_access.permission_level, app_access.status AS access_status,
                  app_access.id AS access_id, app_access.invite_delivery_status,
                  app_access.invite_method
           FROM venue_membership
           JOIN person ON person.id = venue_membership.person_id
           LEFT JOIN venue_role ON venue_role.id = venue_membership.job_role_id
           LEFT JOIN rota_staff_detail ON rota_staff_detail.venue_membership_id = venue_membership.id
           JOIN app_access ON app_access.venue_membership_id = venue_membership.id
               AND app_access.app_id = (SELECT id FROM app WHERE key = 'rotapulse')
           WHERE venue_membership.venue_id = ?
           ORDER BY person.name""",
        (venue_id,),
    ).fetchall()
    staff = []
    for row in rows:
        entry = dict(row)
        entry["display_status"] = _display_status(row["membership_status"], row["access_status"])
        staff.append(entry)
    role_rows = db.execute("SELECT * FROM venue_role WHERE venue_id = ? ORDER BY name", (venue_id,)).fetchall()
    return flask.render_template("admin/staff.html", staff=staff, roles=role_rows)


DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@admin_bp.route("/staff/<int:membership_id>/edit", methods=["GET", "POST"])
@require_permission("app_admin", "rota_admin")
def edit_staff(membership_id):
    """The full record behind a row in the summary staff table — name,
    contact details, date of birth (spec §10's birthday reminder needs
    somewhere for this to actually be entered; nothing did before), role,
    pay rate, home address, and availability, all in one place rather than
    scattered across separate single-field forms."""
    db = get_db()
    venue_id = flask.g.venue["id"]

    membership = db.execute(
        "SELECT * FROM venue_membership WHERE id = ? AND venue_id = ?", (membership_id, venue_id)
    ).fetchone()
    if membership is None:
        flask.abort(404)
    person = db.execute("SELECT * FROM person WHERE id = ?", (membership["person_id"],)).fetchone()
    detail = db.execute(
        "SELECT * FROM rota_staff_detail WHERE venue_membership_id = ?", (membership_id,)
    ).fetchone()
    roles = db.execute("SELECT * FROM venue_role WHERE venue_id = ? ORDER BY name", (venue_id,)).fetchall()
    # Someone with a single invited role (the normal case — every regular
    # staff/rota_admin invite creates exactly one app_access row) can have
    # that permission level changed here. The venue owner is a different
    # shape entirely (auto-provisioned with BOTH app_admin and rota_admin as
    # two separate rows on the same membership) — deliberately not editable
    # through this form, since "the" permission level is ambiguous for them.
    access_rows = db.execute(
        """SELECT * FROM app_access WHERE venue_membership_id = ?
           AND app_id = (SELECT id FROM app WHERE key = 'rotapulse')""",
        (membership_id,),
    ).fetchall()
    editable_access = access_rows[0] if len(access_rows) == 1 else None

    if flask.request.method == "POST":
        form = flask.request.form
        name = form.get("name", "").strip()
        if not name:
            flask.flash("Name is required.", "error")
            return flask.redirect(flask.url_for("admin_config.edit_staff", membership_id=membership_id))

        db.execute(
            "UPDATE person SET name = ?, email = ?, mobile = ?, date_of_birth = ? WHERE id = ?",
            (
                name,
                form.get("email", "").strip() or None,
                form.get("mobile", "").strip() or None,
                form.get("date_of_birth") or None,
                person["id"],
            ),
        )
        db.execute(
            "UPDATE venue_membership SET job_role_id = ? WHERE id = ? AND venue_id = ?",
            (form.get("role_id", type=int) or None, membership_id, venue_id),
        )

        # Permission level, like pay rate below, stays app_admin-only — never
        # trust a client-submitted privilege-sensitive field from a caller
        # who might only be rota_admin. Restricted to 'staff'/'rota_admin':
        # 'app_admin' is the owner-only SSO tier and must never be grantable
        # through this form.
        if "app_admin" in flask.g.permission_levels and editable_access is not None:
            new_level = form.get("permission_level")
            if new_level in ("staff", "rota_admin"):
                db.execute(
                    "UPDATE app_access SET permission_level = ? WHERE id = ?",
                    (new_level, editable_access["id"]),
                )

        availability = {day: bool(form.get(f"available_{day}")) for day in DAYS}
        # Pay rate stays app_admin-only (spec §2.1/§4 — "admin-only, never
        # shown on the staff self-edit screen") — a rota_admin using this
        # same consolidated form must not be able to smuggle a pay-rate
        # change through it by posting the field directly.
        if "app_admin" in flask.g.permission_levels:
            pay_rate = form.get("hourly_pay_rate", type=float) or 0
        else:
            pay_rate = detail["hourly_pay_rate"] if detail else 0
        db.execute(
            """INSERT INTO rota_staff_detail (venue_membership_id, hourly_pay_rate, home_address, availability, start_date)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(venue_membership_id) DO UPDATE SET
               hourly_pay_rate = excluded.hourly_pay_rate,
               home_address = excluded.home_address,
               availability = excluded.availability,
               start_date = excluded.start_date""",
            (
                membership_id, pay_rate, form.get("home_address", "").strip() or None,
                json.dumps(availability), form.get("start_date") or None,
            ),
        )
        db.commit()
        enforce_band(venue_id)
        flask.flash(f"{name}'s record updated.")
        return flask.redirect(flask.url_for("admin_config.staff_list"))

    availability = json.loads(detail["availability"]) if detail and detail["availability"] else {}
    return flask.render_template(
        "admin/staff_edit.html", person=person, membership=membership, detail=detail,
        roles=roles, availability=availability, days=DAYS, editable_access=editable_access,
    )


@admin_bp.route("/staff/create", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def create_staff():
    db = get_db()
    venue_id = flask.g.venue["id"]
    form = flask.request.form
    name = form.get("name", "").strip()
    role_id = form.get("role_id", type=int)
    email = form.get("email", "").strip().lower() or None
    mobile = form.get("mobile", "").strip() or None
    invite_method = form.get("invite_method", "email")
    permission_level = form.get("permission_level", "staff")

    if not name or (not email and not mobile):
        flask.flash("Name and at least one of mobile/email are required.", "error")
        return flask.redirect(flask.url_for("admin_config.staff_list"))
    if permission_level not in ("staff", "rota_admin"):
        flask.abort(400)

    person_cur = db.execute("INSERT INTO person (name, email, mobile) VALUES (?, ?, ?)", (name, email, mobile))
    person_id = person_cur.lastrowid
    membership_cur = db.execute(
        "INSERT INTO venue_membership (person_id, venue_id, job_role_id, status) VALUES (?, ?, ?, 'active')",
        (person_id, venue_id, role_id),
    )
    membership_id = membership_cur.lastrowid

    raw_token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=config.INVITE_TOKEN_EXPIRY_DAYS)).isoformat()
    app_id = get_app_id(db, "rotapulse")
    access_cur = db.execute(
        """INSERT INTO app_access
           (venue_membership_id, app_id, permission_level, status, invite_method,
            invite_token_hash, invite_expires_at, invited_at)
           VALUES (?, ?, ?, 'invited', ?, ?, ?, datetime('now'))""",
        (membership_id, app_id, permission_level, invite_method, _hash_token(raw_token), expires_at),
    )
    db.commit()

    delivery_status = _send_invite_message(flask.g.venue, flask.g.slug, invite_method, email, mobile, raw_token)
    _record_delivery_status(db, access_cur.lastrowid, delivery_status)

    if delivery_status == "failed":
        flask.flash(
            f"Invited {name}, but the {invite_method} couldn't be delivered — "
            "check their details and use Resend invite once fixed.", "error",
        )
    else:
        flask.flash(f"Invited {name}.")
    return flask.redirect(flask.url_for("admin_config.staff_list"))


def _send_invite_message(venue, slug, invite_method, email, mobile, raw_token):
    """Returns 'sent', 'failed', or None (neither contact method was
    actually usable — shouldn't happen given create_staff/resend_invite's
    own validation, but handled rather than assumed)."""
    invite_url = flask.url_for("onboarding.accept", slug=slug, token=raw_token, _external=True)
    message = (
        f"You've been invited to {venue['name']}'s RotaPulse rota. "
        f"Complete your profile here (link expires in {config.INVITE_TOKEN_EXPIRY_DAYS} days): {invite_url}"
    )
    if invite_method == "sms" and mobile:
        delivered = send_sms(mobile, message)
    elif email:
        delivered = send_email(email, f"You've been invited to {venue['name']} on RotaPulse", message)
    else:
        return None
    return "sent" if delivered else "failed"


def _record_delivery_status(db, access_id, delivery_status):
    if delivery_status is None:
        return
    db.execute("UPDATE app_access SET invite_delivery_status = ? WHERE id = ?", (delivery_status, access_id))
    db.commit()


@admin_bp.route("/staff/<int:access_id>/resend-invite", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def resend_invite(access_id):
    """For someone stuck at 'invited' — most often because the original
    email/SMS never actually arrived (e.g. the SMS-delivery-failure class of
    bug this was added alongside) rather than them just not having gotten
    round to it yet. Issues a fresh token + expiry rather than resending the
    old one, which also naturally invalidates whatever link they may already
    have half-used. Optionally switches delivery method (email<->sms) via
    the submitted invite_method — the original invite might have used a
    method that just doesn't work for this person."""
    db = get_db()
    venue_id = flask.g.venue["id"]
    row = db.execute(
        """SELECT app_access.id, app_access.status, app_access.invite_method,
                  person.name, person.email, person.mobile
           FROM app_access
           JOIN venue_membership ON venue_membership.id = app_access.venue_membership_id
           JOIN person ON person.id = venue_membership.person_id
           WHERE app_access.id = ? AND venue_membership.venue_id = ?
           AND app_access.app_id = (SELECT id FROM app WHERE key = 'rotapulse')""",
        (access_id, venue_id),
    ).fetchone()
    if row is None or row["status"] != "invited":
        flask.abort(404)

    # Defaults to whatever method the invite already used, but lets the
    # owner switch — e.g. an email that never arrived, resent by text
    # instead — rather than being permanently locked to the original choice.
    requested_method = flask.request.form.get("invite_method", row["invite_method"])
    if requested_method not in ("email", "sms"):
        flask.abort(400)
    if requested_method == "sms" and not row["mobile"]:
        flask.flash(f"{row['name']} has no mobile number on file — add one before resending by SMS.", "error")
        return flask.redirect(flask.url_for("admin_config.staff_list"))
    if requested_method == "email" and not row["email"]:
        flask.flash(f"{row['name']} has no email on file — add one before resending by email.", "error")
        return flask.redirect(flask.url_for("admin_config.staff_list"))

    raw_token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=config.INVITE_TOKEN_EXPIRY_DAYS)).isoformat()
    db.execute(
        """UPDATE app_access SET invite_token_hash = ?, invite_expires_at = ?, invited_at = datetime('now'),
           invite_delivery_status = NULL, invite_method = ?
           WHERE id = ?""",
        (_hash_token(raw_token), expires_at, requested_method, access_id),
    )
    db.commit()

    delivery_status = _send_invite_message(
        flask.g.venue, flask.g.slug, requested_method, row["email"], row["mobile"], raw_token
    )
    _record_delivery_status(db, access_id, delivery_status)

    if delivery_status == "failed":
        flask.flash(
            f"Resent to {row['name']}, but it still couldn't be delivered — "
            "double-check their contact details.", "error",
        )
    else:
        flask.flash(f"Invite resent to {row['name']}.")
    return flask.redirect(flask.url_for("admin_config.staff_list"))




@admin_bp.route("/staff/pending")
@require_permission("app_admin", "rota_admin")
def pending_approval():
    db = get_db()
    rows = db.execute(
        """SELECT app_access.id AS access_id, venue_membership.id AS membership_id,
                  person.name, person.email, person.mobile
           FROM app_access
           JOIN venue_membership ON venue_membership.id = app_access.venue_membership_id
           JOIN person ON person.id = venue_membership.person_id
           WHERE venue_membership.venue_id = ? AND app_access.status = 'pending_approval'
           AND app_access.app_id = (SELECT id FROM app WHERE key = 'rotapulse')""",
        (flask.g.venue["id"],),
    ).fetchall()
    return flask.render_template("admin/pending.html", pending=rows)


@admin_bp.route("/staff/<int:access_id>/approve", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def approve_staff(access_id):
    db = get_db()
    venue_id = flask.g.venue["id"]
    # Scope to the caller's venue via venue_membership — the app_access row
    # itself has no venue_id, so without this an admin at one venue could
    # approve an access row belonging to another venue by id (IDOR). Mirrors
    # the venue-scoping mark_left/edit_staff/erase_staff already apply. Also
    # needed here (not just for the UPDATE's WHERE) so there's contact detail
    # to actually send the welcome message to.
    row = db.execute(
        """SELECT app_access.invite_method, person.name, person.email, person.mobile
           FROM app_access
           JOIN venue_membership ON venue_membership.id = app_access.venue_membership_id
           JOIN person ON person.id = venue_membership.person_id
           WHERE app_access.id = ? AND venue_membership.venue_id = ?
           AND app_access.app_id = (SELECT id FROM app WHERE key = 'rotapulse')""",
        (access_id, venue_id),
    ).fetchone()
    if row is None:
        flask.abort(404)

    # status = 'pending_approval' in the WHERE stops a re-approve (e.g. a
    # doubled-up click) from re-sending the welcome message.
    cur = db.execute(
        "UPDATE app_access SET status = 'active', approved_at = datetime('now') WHERE id = ? AND status = 'pending_approval'",
        (access_id,),
    )
    if cur.rowcount == 0:
        flask.abort(404)
    db.commit()
    enforce_band(venue_id)

    _send_approval_message(flask.g.venue, flask.g.slug, row["invite_method"], row["email"], row["mobile"])

    flask.flash("Staff member approved — they can now log in and clock in.")
    return flask.redirect(flask.url_for("admin_config.pending_approval"))


def _send_approval_message(venue, slug, invite_method, email, mobile):
    login_url = flask.url_for("rota_login.login", slug=slug, _external=True)
    message = (
        f"Welcome to the team! Your RotaPulse account for {venue['name']} has been approved — you're all set.\n\n"
        "You can now log in to view your upcoming shifts, clock in and out, see and claim open shifts, "
        "request leave, and swap shifts with colleagues.\n\n"
        f"Log in here: {login_url}"
    )
    if invite_method == "sms" and mobile:
        send_sms(mobile, message)
    elif email:
        send_email(email, f"You're approved for {venue['name']} on RotaPulse", message)


@admin_bp.route("/staff/<int:membership_id>/leave", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def mark_left(membership_id):
    db = get_db()
    db.execute(
        "UPDATE venue_membership SET status = 'left' WHERE id = ? AND venue_id = ?",
        (membership_id, flask.g.venue["id"]),
    )
    db.execute(
        """UPDATE app_access SET status = 'revoked'
           WHERE venue_membership_id = ? AND app_id = (SELECT id FROM app WHERE key = 'rotapulse')""",
        (membership_id,),
    )
    db.commit()
    enforce_band(flask.g.venue["id"])
    flask.flash("Staff member marked as left — their access is revoked and they no longer count toward billing.")
    return flask.redirect(flask.url_for("admin_config.staff_list"))


@admin_bp.route("/staff/<int:membership_id>/reinstate", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def reinstate_staff(membership_id):
    """Undoes mark_left — e.g. it was clicked by mistake. Restores both the
    membership and their RotaPulse access straight back to active (not
    whatever intermediate state, like 'invited', it might have been in
    before) — reinstating means they're a working staff member again now,
    able to log in and clock in immediately."""
    db = get_db()
    venue_id = flask.g.venue["id"]
    cur = db.execute(
        "UPDATE venue_membership SET status = 'active' WHERE id = ? AND venue_id = ? AND status = 'left'",
        (membership_id, venue_id),
    )
    if cur.rowcount == 0:
        flask.abort(404)
    db.execute(
        """UPDATE app_access SET status = 'active'
           WHERE venue_membership_id = ? AND app_id = (SELECT id FROM app WHERE key = 'rotapulse')""",
        (membership_id,),
    )
    db.commit()
    enforce_band(venue_id)
    flask.flash("Staff member reinstated — they can log in and clock in again.")
    return flask.redirect(flask.url_for("admin_config.staff_list"))


@admin_bp.route("/staff/<int:membership_id>/erase", methods=["POST"])
@require_permission("app_admin")
def erase_staff(membership_id):
    if erase_person_sensitive_data(flask.g.venue["id"], membership_id):
        flask.flash("Sensitive personal data erased. Hours/pay history is retained for payroll records.")
    else:
        flask.flash("Can only erase data for a staff member who has already left.", "error")
    return flask.redirect(flask.url_for("admin_config.staff_list"))
