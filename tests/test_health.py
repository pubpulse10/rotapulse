"""
The /health deploy-verification endpoint.

The point of this endpoint is to answer WITHOUT a session — every app here
gates essentially everything behind the shared PubPulse SSO, so a health check
that redirects to a login page can only ever tell you the login page is up.
That exemption is easy to lose in a future auth change, which is what these
tests are guarding.
"""

APP_KEY = "rotapulse"


def test_health_answers_without_a_session(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["app"] == APP_KEY
    assert body["status"] == "ok"


def test_health_reports_a_commit(client):
    """RENDER_GIT_COMMIT is absent locally, so 'unknown' is the expected
    value here — what matters is that the key is present and short enough to
    compare against `git log` at a glance."""
    body = client.get("/health").get_json()
    assert "commit" in body
    assert len(body["commit"]) <= 12


def test_health_leaks_no_configuration(client):
    """Unauthenticated, so it must stay minimal. If someone adds environment,
    database or version detail here, this fails on purpose."""
    assert set(client.get("/health").get_json()) == {"app", "commit", "status"}


def test_healthz_is_ok_without_a_session(client):
    """Render's Health Check Path. Must answer with no session, or every
    instance looks unhealthy and gets restarted in a loop."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_healthz_is_503_when_the_database_is_gone(client, tmp_path, monkeypatch):
    """A lost or unmounted disk must fail the check — not quietly create a
    fresh empty database and report healthy."""
    from app import db as db_module
    missing = tmp_path / "unmounted" / "gone.db"
    monkeypatch.setattr(db_module, "DB_PATH", missing)
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.get_json() == {"status": "unavailable"}
    assert not missing.exists()


def test_healthz_leaks_nothing(client):
    assert set(client.get("/healthz").get_json()) == {"status"}
