from app import db as db_module


def _headers():
    return {"Authorization": "Bearer test-secret"}


def test_access_requires_bearer_auth(client, venue):
    resp = client.post("/internal/access", json={"pub_id": venue["pub_id"], "person_id": 1})
    assert resp.status_code == 401


def test_access_creates_a_new_person_and_grant(app, client, venue, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    resp = client.post(
        "/internal/access",
        json={"pub_id": venue["pub_id"], "person_id": 999, "name": "New Manager",
              "email": "manager@example.com", "level": "manager", "status": "active"},
        headers=_headers(),
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        person = conn.execute("SELECT * FROM person WHERE hub_person_id = 999").fetchone()
        assert person is not None
        assert person["email"] == "manager@example.com"
        access = conn.execute(
            """SELECT app_access.permission_level, app_access.status FROM app_access
               JOIN venue_membership ON venue_membership.id = app_access.venue_membership_id
               WHERE venue_membership.person_id = ?""",
            (person["id"],),
        ).fetchone()
        assert access["permission_level"] == "rota_admin"  # Hub 'manager' maps to rota_admin
        assert access["status"] == "active"


def test_access_links_to_an_existing_locally_invited_person_by_email(app, client, venue, monkeypatch):
    """The real scenario this guards against: someone invited directly
    through RotaPulse's own staff page (app/admin_config.py::create_staff,
    still fully live) has no hub_person_id at all. If the owner later also
    pushes the same real human through the Hub, this must link the existing
    record rather than create a second, disconnected one."""
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO person (name, email) VALUES (?, ?)", ("Lianne Fairweather", "lianne@example.com")
        )
        local_person_id = cur.lastrowid
        conn.commit()

    resp = client.post(
        "/internal/access",
        json={"pub_id": venue["pub_id"], "person_id": 555, "name": "Lianne Fairweather",
              "email": "lianne@example.com", "level": "staff", "status": "active"},
        headers=_headers(),
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        rows = conn.execute("SELECT * FROM person WHERE email = 'lianne@example.com'").fetchall()
        assert len(rows) == 1  # linked, not duplicated
        assert rows[0]["id"] == local_person_id
        assert rows[0]["hub_person_id"] == 555


def test_access_matches_email_case_insensitively(app, client, venue, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO person (name, email) VALUES (?, ?)", ("Casey", "casey@example.com")
        )
        local_person_id = cur.lastrowid
        conn.commit()

    resp = client.post(
        "/internal/access",
        json={"pub_id": venue["pub_id"], "person_id": 321, "name": "Casey",
              "email": "Casey@Example.com", "level": "staff", "status": "active"},
        headers=_headers(),
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        rows = conn.execute("SELECT * FROM person").fetchall()
        matching = [r for r in rows if r["id"] == local_person_id]
        assert len(matching) == 1
        assert matching[0]["hub_person_id"] == 321


def test_access_does_not_link_a_person_already_linked_to_a_different_hub_id(app, client, venue, monkeypatch):
    """A person whose hub_person_id is already set belongs to a different
    Hub identity -- must never be silently re-linked to a new one, even if
    a (surprising) email collision occurred."""
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO person (name, email, hub_person_id) VALUES (?, ?, ?)",
            ("Existing Hub Person", "shared@example.com", 111),
        )
        conn.commit()

    resp = client.post(
        "/internal/access",
        json={"pub_id": venue["pub_id"], "person_id": 222, "name": "Someone Else",
              "email": "shared@example.com", "level": "staff", "status": "active"},
        headers=_headers(),
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        rows = conn.execute("SELECT * FROM person WHERE email = 'shared@example.com'").fetchall()
        # A new, separate person row for hub_person_id 222 -- the existing
        # hub_person_id 111 row must be untouched, not stolen.
        assert len(rows) == 2
        ids_by_hub = {r["hub_person_id"]: r["id"] for r in rows}
        assert ids_by_hub[111] is not None
        assert ids_by_hub[222] is not None
        assert ids_by_hub[111] != ids_by_hub[222]
