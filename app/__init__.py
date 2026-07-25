import json
import os
from pathlib import Path

import flask
from flask_wtf import CSRFProtect

from app import config, db
from app.date_format import format_uk_date, format_uk_datetime

BASE_DIR = Path(__file__).resolve().parent.parent


def create_app():
    app = flask.Flask(__name__)
    config.apply(app)
    db.init_app(app)
    csrf = CSRFProtect(app)
    app.jinja_env.filters["from_json"] = lambda s: json.loads(s) if s else {}
    app.jinja_env.filters["uk_date"] = format_uk_date
    app.jinja_env.filters["uk_datetime"] = format_uk_datetime

    @app.template_global()
    def static_version(filename):
        """Cache-busting query string for a static asset, based on its own
        mtime — same idiom as the sibling apps."""
        path = Path(app.static_folder) / filename
        try:
            return int(path.stat().st_mtime)
        except OSError:
            return 0

    _LEAVE_ENDPOINTS = {
        "rota_grid.leave_queue",
        "rota_grid.approve_leave",
        "rota_grid.decline_leave",
        "rota_grid.create_leave",
    }

    @app.template_global()
    def nav_section():
        """Which top-nav item the current page belongs to, for underlining
        the active one in base.html. Rota and Leave share a blueprint
        (rota_grid), so those are split out by endpoint name; Staff and
        Settings likewise share admin_config."""
        endpoint = flask.request.endpoint or ""
        if endpoint in _LEAVE_ENDPOINTS:
            return "leave"
        if endpoint.startswith("rota_grid."):
            return "rota"
        if endpoint == "admin_config.settings":
            return "settings"
        if endpoint.startswith("admin_config."):
            return "staff"
        if endpoint.startswith("payroll."):
            return "payroll"
        if endpoint.startswith("dashboard."):
            return "dashboard"
        if endpoint.startswith("billing."):
            return "subscription"
        if endpoint.startswith("staff_portal."):
            return "my_shifts"
        return None

    from app.admin_config import admin_bp
    from app.billing import billing_bp, register_webhook
    from app.dashboard import dashboard_bp
    from app.internal import internal_bp
    from app.media import media_bp
    from app.onboarding import onboard_bp
    from app.payroll import payroll_bp
    from app.rota_grid import rota_bp
    from app.rota_login import login_bp
    from app.staff_portal import staff_bp
    from app.venues import venues_bp

    app.register_blueprint(venues_bp)
    app.register_blueprint(onboard_bp)
    app.register_blueprint(login_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(rota_bp)
    app.register_blueprint(staff_bp)
    app.register_blueprint(payroll_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(billing_bp)
    app.register_blueprint(media_bp)

    # Server-to-server only (bearer-secret authed, no session/CSRF token) —
    # exempted the same way the Stripe webhook is below.
    csrf.exempt(internal_bp)
    app.register_blueprint(internal_bp)

    webhook_view = register_webhook(app)
    csrf.exempt(webhook_view)

    if os.environ.get("FLASK_ENV") != "production":
        @app.route("/dev-login")
        def dev_login():
            """Local-dev-only convenience: sets session['pub_id'] directly,
            simulating an already-authenticated shared PubPulse session
            without a real cross-app PricePulse login — there's no other way
            to reach the owner-only pages on a laptop with no real PubPulse
            account. Never registered when FLASK_ENV=production."""
            pub_id = flask.request.args.get("pub_id", type=int)
            if pub_id is None:
                return "Usage: /dev-login?pub_id=0 (matches scripts/init_db.py's seeded dev venue)", 400
            flask.session.permanent = True
            flask.session["pub_id"] = pub_id
            flask.session["landlord_email"] = flask.request.args.get("email", "dev@example.com")
            return flask.redirect(flask.url_for("venues.entry"))

    @app.route("/sw.js")
    def service_worker():
        resp = flask.send_file(
            BASE_DIR / "app" / "static" / "sw.js", mimetype="application/javascript"
        )
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    return app
