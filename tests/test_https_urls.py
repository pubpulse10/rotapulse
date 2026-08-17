"""Every external URL this app hands out must be https in production —
Stripe Checkout's success_url/cancel_url above all, since a customer follows
those the instant after entering card details, but also the staff invite
links, password-reset links and the open-shift claim URLs texted to staff.

These exercise app.config.pin_https_scheme against a bare app wired the same
way create_app() wires the real one (ProxyFix, then the pin), rather than
building a production-mode RotaPulse, which would need the real secrets.

Note the limit of a local test: it can prove the pin overrides whatever the
header says, but only a live check proves what the real proxy chain sends.
That distinction is what let the rate-limiter bug in app/extensions.py pass
every local test while being broken in production.

Mirrored in PricePulse and TaskPulse — change one, change all.
"""

import flask
import pytest
from werkzeug.middleware.proxy_fix import ProxyFix

from app import config


def _wired_app(monkeypatch, flask_env):
    if flask_env is None:
        monkeypatch.delenv("FLASK_ENV", raising=False)
    else:
        monkeypatch.setenv("FLASK_ENV", flask_env)

    app = flask.Flask(__name__)

    @app.route("/billing/success")
    def success():
        return flask.url_for("success", _external=True)

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    config.pin_https_scheme(app)
    return app


def _generated_url(app, forwarded_proto):
    headers = {} if forwarded_proto is None else {"X-Forwarded-Proto": forwarded_proto}
    return app.test_client().get("/billing/success", headers=headers).get_data(as_text=True)


# `None` is the case that actually bit production: waitress runs with
# clear_untrusted_proxy_headers=True and no trusted_proxy, so it strips
# X-Forwarded-* before the app is called and ProxyFix has nothing to read.
# The others cover a client trying to force the scheme downward, and the
# multi-hop list shape, so the pin is unconditional either way.
@pytest.mark.parametrize("forwarded_proto", [None, "http", "https", "https,http"])
def test_external_urls_are_https_in_production(monkeypatch, forwarded_proto):
    url = _generated_url(_wired_app(monkeypatch, "production"), forwarded_proto)
    assert url.startswith("https://"), url


def test_scheme_is_not_pinned_outside_production(monkeypatch):
    """Local development is served over plain http; pinning https there would
    generate links that don't resolve."""
    url = _generated_url(_wired_app(monkeypatch, None), None)
    assert url.startswith("http://"), url


def test_preferred_url_scheme_set_for_requestless_url_for(monkeypatch):
    """url_for(_external=True) outside a request context (the shift-notification
    cron entrypoint) has no header to read and falls back to this config."""
    app = _wired_app(monkeypatch, "production")
    assert app.config["PREFERRED_URL_SCHEME"] == "https"


def test_create_app_wires_the_pin():
    """Guards against the helper existing but never being called — the
    failure mode that would silently restore the original bug."""
    import inspect

    from app import create_app

    assert "pin_https_scheme" in inspect.getsource(create_app)
