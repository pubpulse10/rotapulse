import json
import os
from pathlib import Path

import flask
from flask_wtf import CSRFProtect

from app import config, db
from app.date_format import format_uk_date, format_uk_datetime, format_uk_time, variance_label

BASE_DIR = Path(__file__).resolve().parent.parent


def create_app():
    app = flask.Flask(__name__)
    config.apply(app)

    # Behind Waitress + a reverse proxy: trust exactly one proxy hop for the
    # client IP and scheme, so the rate limiter keys on the real remote
    # address (X-Forwarded-For) rather than the proxy's, and url_for(_external)
    # honours X-Forwarded-Proto.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    # ...except waitress strips X-Forwarded-* before ProxyFix can read them,
    # so that alone leaves url_for(_external) on http. After ProxyFix.
    config.pin_https_scheme(app)

    # In-memory limiter storage is correct for a single Waitress process (the
    # current deployment). A multi-instance/multi-process deployment would
    # need a shared backend (e.g. redis) so limits are enforced globally.
    from app.extensions import limiter
    limiter.init_app(app)

    # Cap request bodies so an oversized upload can't exhaust memory/disk
    # (covers the avatar/attendance-photo uploads in app/media.py).
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

    db.init_app(app)
    csrf = CSRFProtect(app)
    app.jinja_env.filters["from_json"] = lambda s: json.loads(s) if s else {}
    app.jinja_env.filters["uk_date"] = format_uk_date
    app.jinja_env.filters["uk_datetime"] = format_uk_datetime
    app.jinja_env.filters["uk_time"] = format_uk_time
    app.jinja_env.filters["variance_label"] = variance_label

    @app.context_processor
    def inject_hub_url():
        # An owner session here isn't a RotaPulse-specific login at all —
        # it's the same shared PubPulse session cookie the sibling apps and
        # the Hub all read. "My Apps" just links back to it.
        return {"pubpulse_hub_url": config.PUBPULSE_HUB_URL}

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
    from app.family_admin import family_admin_bp
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
    app.register_blueprint(family_admin_bp)

    @app.route("/favicon.ico")
    def favicon():
        """Serve the icon at the domain root too, not just via the <link> in
        <head>. Windows .url desktop shortcuts, bookmarks and crawlers request
        /favicon.ico directly and ignore the page markup — without this they
        404 and fall back to a generic icon."""
        return app.send_static_file("icons/favicon.ico")

    @app.route("/robots.txt")
    def robots_txt():
        """Search engines should index the marketing site at
        www.pubpulse.co.uk only — never this application host. Served from a
        route rather than a static file so it works identically on Render
        regardless of how static assets are mounted."""
        return app.response_class(
            "User-agent: *\nDisallow: /\n", mimetype="text/plain"
        )

    @app.route("/health")
    def health():
        """Deploy verification: reports the commit this instance is actually
        running, so a deploy can be confirmed rather than assumed. Render sets
        RENDER_GIT_COMMIT automatically; it's absent locally, hence 'unknown'.

        Unauthenticated on purpose (same reasoning as /robots.txt — it has to
        answer without a session to be useful), so it is deliberately minimal:
        an app name and a commit, and nothing about configuration, environment
        or data. Keep it that way. Identical in all five family apps."""
        return {
            "app": "rotapulse",
            "commit": os.environ.get("RENDER_GIT_COMMIT", "unknown")[:12],
            "status": "ok",
        }

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

    @app.after_request
    def set_security_headers(resp):
        # Baseline hardening headers. setdefault() so a view that sets its own
        # header (e.g. the service worker above) is never overridden.
        # Application host, not the marketing site: keep every response out of
        # search results. robots.txt stops the crawl; this header stops the URL
        # being listed even if a crawler finds it linked from somewhere else.
        resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # HSTS only when cookies are already HTTPS-only (production). A
        # Content-Security-Policy is deliberately NOT enforced here yet — it
        # needs testing against inline scripts and the PWA service worker.
        if app.config.get("SESSION_COOKIE_SECURE"):
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        # Report-only CSP: browsers evaluate and report violations but never
        # block, so this can't break inline scripts, the PWA service worker or
        # the Stripe.js embed. Tune against real violation reports, then
        # promote to an enforcing Content-Security-Policy header.
        resp.headers.setdefault(
            "Content-Security-Policy-Report-Only",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline' https://js.stripe.com; "
            "frame-src https://js.stripe.com; object-src 'none'; base-uri 'self'",
        )
        return resp

    return app
