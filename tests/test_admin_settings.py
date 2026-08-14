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


def test_holiday_year_start_day_month_dropdowns_save_correctly(app, client, venue):
    """The field was originally free text expecting an exact 'MM-DD' the
    landlord had to remember to type, including a literal dash — a real
    saved value of '0101' (no dash) crashed every staff member's leave page
    at that venue (see test_leave.py). Two validated dropdowns make a
    malformed value structurally impossible instead of needing to detect
    one, and read in UK day-then-month order rather than the internal
    storage convention."""
    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/admin/settings",
        data={"venue_name": "Test Venue", "holiday_year_start_day": "6", "holiday_year_start_month": "4"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT holiday_year_start_date FROM venue_settings WHERE venue_id = ?", (venue["id"],)
        ).fetchone()
        assert row["holiday_year_start_date"] == "04-06"  # stored MM-DD; 6 April as entered day-then-month


def test_holiday_year_start_rejects_an_impossible_day_month_combo(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/admin/settings",
        data={"venue_name": "Test Venue", "holiday_year_start_day": "30", "holiday_year_start_month": "2"},
        follow_redirects=True,
    )
    assert b"not a real day and month" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT holiday_year_start_date FROM venue_settings WHERE venue_id = ?", (venue["id"],)
        ).fetchone()
        assert row["holiday_year_start_date"] == "01-01"  # unchanged from conftest's seeded value


def test_holiday_year_start_requires_both_day_and_month_together(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/admin/settings",
        data={"venue_name": "Test Venue", "holiday_year_start_day": "6"},  # month left blank
        follow_redirects=True,
    )
    assert b"both a day and a month" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT holiday_year_start_date FROM venue_settings WHERE venue_id = ?", (venue["id"],)
        ).fetchone()
        assert row["holiday_year_start_date"] == "01-01"  # unchanged


def test_holiday_year_start_can_be_cleared(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/admin/settings",
        data={"venue_name": "Test Venue"},  # both day and month left blank
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT holiday_year_start_date FROM venue_settings WHERE venue_id = ?", (venue["id"],)
        ).fetchone()
        assert row["holiday_year_start_date"] is None


def test_settings_page_preselects_the_stored_day_and_month(app, client, venue):
    """Stored internally as '04-06' (MM-DD) — the day dropdown should show
    6 selected and the month dropdown April, not the raw stored order."""
    login_as_pub(client, venue["pub_id"])
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE venue_settings SET holiday_year_start_date = '04-06' WHERE venue_id = ?", (venue["id"],)
        )
        conn.commit()

    resp = client.get(f"/v/{venue['slug']}/admin/settings")
    html = resp.data.decode()
    day_select = html.split('name="holiday_year_start_day"')[1].split("</select>")[0]
    month_select = html.split('name="holiday_year_start_month"')[1].split("</select>")[0]
    assert '<option value="6" selected' in day_select
    assert '<option value="4" selected' in month_select


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
