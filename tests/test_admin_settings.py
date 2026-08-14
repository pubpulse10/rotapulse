from app import db as db_module
from tests.conftest import login_as_pub


def test_venue_name_and_postcode_can_be_updated(app, client, venue, monkeypatch):
    from app import admin_config

    monkeypatch.setattr(admin_config, "geocode_postcode", lambda postcode: (51.5, -0.1))
    login_as_pub(client, venue["pub_id"])

    resp = client.post(
        f"/v/{venue['slug']}/admin/settings",
        data={"venue_name": "The Renamed Arms", "postcode": "SW1A 1AA"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"The Renamed Arms" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT * FROM venue WHERE id = ?", (venue["id"],)).fetchone()
        assert row["name"] == "The Renamed Arms"
        assert row["postcode"] == "SW1A 1AA"
        assert row["latitude"] == 51.5
        assert row["longitude"] == -0.1


def test_unchanged_postcode_does_not_trigger_geocoding(app, client, venue, monkeypatch):
    from app import admin_config

    calls = []
    monkeypatch.setattr(admin_config, "geocode_postcode", lambda postcode: calls.append(postcode) or (99, 99))
    login_as_pub(client, venue["pub_id"])

    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE venue SET postcode = 'NR20 3EN' WHERE id = ?", (venue["id"],))
        conn.commit()

    client.post(
        f"/v/{venue['slug']}/admin/settings",
        data={"venue_name": venue.get("name", "Test Venue"), "postcode": "nr20 3en"},  # same postcode, different case
    )
    assert calls == []  # no geocoding call — postcode didn't actually change


def test_failed_geocode_keeps_previous_coordinates_but_still_saves_name(app, client, venue, monkeypatch):
    from app import admin_config

    monkeypatch.setattr(admin_config, "geocode_postcode", lambda postcode: None)
    login_as_pub(client, venue["pub_id"])

    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE venue SET latitude = 52.6, longitude = 1.3 WHERE id = ?", (venue["id"],))
        conn.commit()

    resp = client.post(
        f"/v/{venue['slug']}/admin/settings",
        data={"venue_name": "Renamed Only", "postcode": "BADPOSTCODE"},
        follow_redirects=True,
    )
    assert b"couldn" in resp.data.lower()

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT * FROM venue WHERE id = ?", (venue["id"],)).fetchone()
        assert row["name"] == "Renamed Only"
        assert row["latitude"] == 52.6  # unchanged, not wiped out
        assert row["longitude"] == 1.3


def test_holiday_year_start_date_rejects_unparseable_value(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/admin/settings",
        data={"venue_name": "Test Venue", "holiday_year_start_date": "not a date"},
        follow_redirects=True,
    )
    assert b"day and month" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT holiday_year_start_date FROM venue_settings WHERE venue_id = ?", (venue["id"],)
        ).fetchone()
        assert row["holiday_year_start_date"] == "01-01"  # unchanged from conftest's seeded value


def test_holiday_year_start_date_is_normalized_on_save(app, client, venue):
    """The real bug: a venue owner saved '0101' (no dash) here with no
    validation, which later crashed every staff member's leave page (see
    test_leave.py's malformed-year-start regression test). Now normalized
    to the canonical MM-DD rather than either crashing or being rejected
    outright — the landlord shouldn't have to remember an exact format."""
    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/admin/settings",
        data={"venue_name": "Test Venue", "holiday_year_start_date": "0101"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT holiday_year_start_date FROM venue_settings WHERE venue_id = ?", (venue["id"],)
        ).fetchone()
        assert row["holiday_year_start_date"] == "01-01"


def test_holiday_year_start_date_accepts_slash_and_single_digits(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/admin/settings",
        data={"venue_name": "Test Venue", "holiday_year_start_date": "4/6"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT holiday_year_start_date FROM venue_settings WHERE venue_id = ?", (venue["id"],)
        ).fetchone()
        assert row["holiday_year_start_date"] == "04-06"


def test_empty_venue_name_is_rejected(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/admin/settings",
        data={"venue_name": "", "postcode": ""},
        follow_redirects=True,
    )
    assert b"required" in resp.data.lower()

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT name FROM venue WHERE id = ?", (venue["id"],)).fetchone()
        assert row["name"] == "Test Venue"  # unchanged
