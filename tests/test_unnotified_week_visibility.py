"""
"Staff can see shifts before the week is notified" (venue setting).

Customer question, 2026-11-04: staff could see a week's rota while the
manager was still drafting it, because "Notify staff" only ever sent the
messages -- nothing on the staff side looked at it. The venue now chooses.
On (the default) is how every venue has always worked; off hides a week
until it has been notified, except a shift dated today or earlier, which
always shows so a forgotten Notify can't stop anyone clocking in or out.
"""

from datetime import timedelta

from app import db as db_module
from app.rota_grid import _monday_of
from app.uk_time import uk_today
from tests.conftest import create_active_staff, login_as_person, login_as_pub


def _shift(app, venue_id, person_id, shift_date, start="09:00", end="17:00", status="scheduled"):
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, ?, ?, ?)",
            (venue_id, person_id, shift_date.isoformat(), start, end, status),
        )
        conn.commit()
        return cur.lastrowid


def _hide_unnotified(app, venue, hide=True):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE venue_settings SET staff_see_unnotified_weeks = ? WHERE venue_id = ?",
                     (0 if hide else 1, venue["id"]))
        conn.commit()


def _mark_notified(app, venue, week_start):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("INSERT INTO week_notification (venue_id, week_start_date) VALUES (?, ?)",
                     (venue["id"], week_start.isoformat()))
        conn.commit()


def _next_week_wednesday():
    return _monday_of(uk_today()) + timedelta(days=9)


def test_default_is_on_so_staff_still_see_an_unnotified_week(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Default Dan")
    day = _next_week_wednesday()
    _shift(app, venue["id"], person_id, day, start="10:15", end="16:45")

    login_as_person(client, person_id)
    assert b"10:15" in client.get(f"/v/{venue['slug']}/staff/").data
    assert b"10:15" in client.get(f"/v/{venue['slug']}/staff/rota?week={day.isoformat()}").data


def test_switched_off_hides_an_unnotified_week_everywhere(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Hidden Hal")
    colleague_id, _m2, _e2 = create_active_staff(app, venue["id"], name="Colleague Cat")
    day = _next_week_wednesday()
    own_id = _shift(app, venue["id"], person_id, day, start="10:15", end="16:45")
    _shift(app, venue["id"], colleague_id, day, start="11:20", end="15:40")
    _hide_unnotified(app, venue)

    login_as_person(client, person_id)
    assert b"10:15" not in client.get(f"/v/{venue['slug']}/staff/").data
    rota = client.get(f"/v/{venue['slug']}/staff/rota?week={day.isoformat()}")
    assert rota.status_code == 200
    assert b"10:15" not in rota.data and b"11:20" not in rota.data
    assert b"sent out yet" in rota.data
    assert client.get(f"/v/{venue['slug']}/staff/shift/{own_id}").status_code == 404
    swap = client.post(f"/v/{venue['slug']}/staff/shift/{own_id}/swap/request", data={"to_person_id": colleague_id})
    assert swap.status_code == 404


def test_switched_off_shows_the_week_once_notified(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Notified Nell")
    day = _next_week_wednesday()
    own_id = _shift(app, venue["id"], person_id, day, start="10:15", end="16:45")
    _hide_unnotified(app, venue)
    _mark_notified(app, venue, _monday_of(day))

    login_as_person(client, person_id)
    assert b"10:15" in client.get(f"/v/{venue['slug']}/staff/").data
    assert b"10:15" in client.get(f"/v/{venue['slug']}/staff/rota?week={day.isoformat()}").data
    assert client.get(f"/v/{venue['slug']}/staff/shift/{own_id}").status_code == 200


def test_todays_shift_always_shows_so_it_can_be_clocked_into(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Today Tom")
    own_id = _shift(app, venue["id"], person_id, uk_today(), start="00:05", end="23:55")
    _hide_unnotified(app, venue)

    login_as_person(client, person_id)
    assert b"00:05" in client.get(f"/v/{venue['slug']}/staff/").data
    assert client.get(f"/v/{venue['slug']}/staff/shift/{own_id}").status_code == 200


def test_settings_page_saves_the_switch(app, client, venue, monkeypatch):
    from app import admin_config

    monkeypatch.setattr(admin_config, "geocode_postcode", lambda postcode: (51.5, -0.1))
    login_as_pub(client, venue["pub_id"])
    page = client.get(f"/v/{venue['slug']}/admin/settings")
    assert b"Staff can see shifts before the week is notified" in page.data

    def saved():
        with app.app_context():
            return db_module.get_db().execute(
                "SELECT staff_see_unnotified_weeks FROM venue_settings WHERE venue_id = ?", (venue["id"],)
            ).fetchone()[0]

    client.post(f"/v/{venue['slug']}/admin/settings", data={"venue_name": "The Arms"})
    assert saved() == 0
    client.post(f"/v/{venue['slug']}/admin/settings",
                data={"venue_name": "The Arms", "staff_see_unnotified_weeks": "on"})
    assert saved() == 1
