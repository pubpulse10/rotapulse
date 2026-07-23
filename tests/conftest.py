import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SECRET_KEY", "test-secret-key")

import pytest
from werkzeug.security import generate_password_hash

from app import create_app, db as db_module

TEST_PUB_ID = 42


@pytest.fixture
def app(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_path = Path(path)
    monkeypatch.setattr(db_module, "DB_PATH", db_path)

    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False

    with application.app_context():
        db_module.init_schema()

    yield application

    db_path.unlink(missing_ok=True)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def venue(app):
    """An active, fully-provisioned venue with an owner (app_admin +
    rota_admin, SSO-only, no password) already set up — the equivalent of
    what venues.setup() would have created."""
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO venue (pub_id, name, slug, latitude, longitude) VALUES (?, ?, ?, ?, ?)",
            (TEST_PUB_ID, "Test Venue", "testvenue", 52.6, 1.3),
        )
        venue_id = cur.lastrowid
        conn.execute(
            "INSERT INTO venue_settings (venue_id, target_staff_cost_percent, holiday_year_start_date) VALUES (?, 28, '01-01')",
            (venue_id,),
        )
        conn.execute("INSERT INTO rota_subscription (venue_id, plan) VALUES (?, 'active')", (venue_id,))

        owner_cur = conn.execute(
            "INSERT INTO person (name, email, pub_id) VALUES (?, ?, ?)", ("Owner Person", "owner@example.com", TEST_PUB_ID)
        )
        owner_person_id = owner_cur.lastrowid
        membership_cur = conn.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')",
            (owner_person_id, venue_id),
        )
        owner_membership_id = membership_cur.lastrowid
        app_id = db_module.get_app_id(conn, "rotapulse")
        for level in ("app_admin", "rota_admin"):
            conn.execute(
                """INSERT INTO app_access (venue_membership_id, app_id, permission_level, status, accepted_at, approved_at)
                   VALUES (?, ?, ?, 'active', datetime('now'), datetime('now'))""",
                (owner_membership_id, app_id, level),
            )

        role_cur = conn.execute("INSERT INTO venue_role (venue_id, name) VALUES (?, 'Bar staff')", (venue_id,))
        role_id = role_cur.lastrowid

        conn.commit()

    return {
        "id": venue_id, "slug": "testvenue", "pub_id": TEST_PUB_ID,
        "owner_person_id": owner_person_id, "owner_membership_id": owner_membership_id,
        "role_id": role_id,
    }


def login_as_pub(client, pub_id):
    """Simulates arriving with an already-authenticated shared PubPulse
    session — RotaPulse never issues this cookie itself (PricePulse's login
    does), so tests set it directly rather than going through a real
    cross-app login. Also clears any local RotaPulse staff session, since
    rota_auth checks that first — mirrors a fresh browser session arriving
    via SSO, not the same person somehow holding both identities at once."""
    with client.session_transaction() as sess:
        sess["pub_id"] = pub_id
        sess.pop("rotapulse_person_id", None)


TEST_STAFF_PASSWORD = "correct horse battery"


def create_active_staff(app, venue_id, name="Staff Person", role_id=None, permission_level="staff", email=None):
    """Inserts a fully-active staff member (person + venue_membership +
    app_access status=active + rota_staff_detail), bypassing the invite
    flow — the fast path most tests need. Returns the person row's id and
    the plaintext password for logging in via login_as_person_via_form."""
    from app import db as db_module

    with app.app_context():
        conn = db_module.get_db()
        email = email or f"{name.lower().replace(' ', '')}@example.com"
        person_cur = conn.execute(
            "INSERT INTO person (name, email, password_hash) VALUES (?, ?, ?)",
            (name, email, generate_password_hash(TEST_STAFF_PASSWORD)),
        )
        person_id = person_cur.lastrowid
        membership_cur = conn.execute(
            "INSERT INTO venue_membership (person_id, venue_id, job_role_id, status) VALUES (?, ?, ?, 'active')",
            (person_id, venue_id, role_id),
        )
        membership_id = membership_cur.lastrowid
        app_id = db_module.get_app_id(conn, "rotapulse")
        conn.execute(
            """INSERT INTO app_access (venue_membership_id, app_id, permission_level, status, accepted_at, approved_at)
               VALUES (?, ?, ?, 'active', datetime('now'), datetime('now'))""",
            (membership_id, app_id, permission_level),
        )
        conn.execute(
            "INSERT INTO rota_staff_detail (venue_membership_id, hourly_pay_rate, availability) VALUES (?, ?, ?)",
            (membership_id, 12.5, '{"mon":true,"tue":true,"wed":true,"thu":true,"fri":true,"sat":true,"sun":true}'),
        )
        conn.commit()

    return person_id, membership_id, email


def login_as_person(client, person_id):
    with client.session_transaction() as sess:
        sess["rotapulse_person_id"] = person_id
        sess.pop("pub_id", None)
