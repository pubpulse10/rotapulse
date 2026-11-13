"""
Open shifts are held back with the rest of an unnotified week.

Found 12 November 2026: with "Staff can see shifts before the week is
notified" unticked, staff still saw and claimed the open shifts in a week the
manager was drafting -- the list, the claim page and the claim itself looked
only at status = 'open'. An open shift is now released to staff when its week
is visible to them (tests/test_unnotified_week_visibility.py) or its own
"Notify staff" alert has been sent. See rota_grid.released_open_shift_ids.
"""

from datetime import timedelta

from app import db as db_module
from app.rota_grid import _monday_of
from app.uk_time import uk_today
from tests.conftest import create_active_staff, login_as_person, login_as_pub

NOT_RELEASED = b"available to claim yet"
LABEL = b"Not yet released to staff"


def _open_shift(app, venue_id, shift_date, start="18:35", end="22:25"):
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO shift (venue_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, ?, 'open')",
            (venue_id, shift_date.isoformat(), start, end),
        )
        conn.commit()
        return cur.lastrowid


def _hide_unnotified(app, venue):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE venue_settings SET staff_see_unnotified_weeks = 0 WHERE venue_id = ?", (venue["id"],))
        conn.commit()


def _mark_week_notified(app, venue, week_start):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("INSERT INTO week_notification (venue_id, week_start_date) VALUES (?, ?)",
                     (venue["id"], week_start.isoformat()))
        conn.commit()


def _shift_state(app, shift_id):
    with app.app_context():
        row = db_module.get_db().execute("SELECT status, person_id FROM shift WHERE id = ?", (shift_id,)).fetchone()
        return row["status"], row["person_id"]


def _next_week_wednesday():
    return _monday_of(uk_today()) + timedelta(days=9)


def test_default_setting_leaves_open_shifts_exactly_as_they_were(app, client, venue):
    """Ticked is the default and how every existing venue runs: an open shift
    in a week nobody has notified is listed and claimable, as before."""
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Default Dan")
    day = _next_week_wednesday()
    shift_id = _open_shift(app, venue["id"], day)

    login_as_person(client, person_id)
    assert b"18:35" in client.get(f"/v/{venue['slug']}/staff/open-shifts").data
    assert b"18:35" in client.get(f"/v/{venue['slug']}/staff/rota?week={day.isoformat()}").data
    assert client.get(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}").status_code == 200
    client.post(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}/claim")
    assert _shift_state(app, shift_id) == ("scheduled", person_id)


def test_unreleased_open_shift_is_hidden_and_cannot_be_claimed(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Hidden Hal")
    day = _next_week_wednesday()
    shift_id = _open_shift(app, venue["id"], day)
    _hide_unnotified(app, venue)

    login_as_person(client, person_id)
    assert b"18:35" not in client.get(f"/v/{venue['slug']}/staff/open-shifts").data
    assert b"18:35" not in client.get(f"/v/{venue['slug']}/staff/rota?week={day.isoformat()}").data

    # A direct link to the claim page, and a direct POST, are both refused.
    prompt = client.get(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}", follow_redirects=True)
    assert NOT_RELEASED in prompt.data
    assert b"18:35" not in prompt.data
    claim = client.post(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}/claim", follow_redirects=True)
    assert NOT_RELEASED in claim.data
    assert _shift_state(app, shift_id) == ("open", None)


def test_todays_unreleased_open_shift_is_hidden_too(app, client, venue):
    """A person's own shift dated today always shows so they can clock in.
    An open shift has no such need, so there is no exception for it."""
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Today Tom")
    shift_id = _open_shift(app, venue["id"], uk_today())
    _hide_unnotified(app, venue)

    login_as_person(client, person_id)
    assert b"18:35" not in client.get(f"/v/{venue['slug']}/staff/open-shifts").data
    client.post(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}/claim")
    assert _shift_state(app, shift_id) == ("open", None)


def test_its_own_alert_releases_an_open_shift_in_an_unnotified_week(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Alerted Al")
    day = _next_week_wednesday()
    alerted_id = _open_shift(app, venue["id"], day)
    other_id = _open_shift(app, venue["id"], day, start="07:05", end="11:55")
    _hide_unnotified(app, venue)

    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/rota/shift/{alerted_id}/notify")

    login_as_person(client, person_id)
    listing = client.get(f"/v/{venue['slug']}/staff/open-shifts").data
    assert b"18:35" in listing
    assert b"07:05" not in listing  # the alert releases that one shift, not the week
    assert client.get(f"/v/{venue['slug']}/staff/open-shifts/{alerted_id}").status_code == 200
    client.post(f"/v/{venue['slug']}/staff/open-shifts/{alerted_id}/claim")
    assert _shift_state(app, alerted_id) == ("scheduled", person_id)
    assert _shift_state(app, other_id) == ("open", None)


def test_notifying_the_week_releases_its_open_shifts(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Notified Nell")
    day = _next_week_wednesday()
    shift_id = _open_shift(app, venue["id"], day)
    _hide_unnotified(app, venue)
    _mark_week_notified(app, venue, _monday_of(day))

    login_as_person(client, person_id)
    assert b"18:35" in client.get(f"/v/{venue['slug']}/staff/open-shifts").data
    assert b"18:35" in client.get(f"/v/{venue['slug']}/staff/rota?week={day.isoformat()}").data
    client.post(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}/claim")
    assert _shift_state(app, shift_id) == ("scheduled", person_id)


def test_admin_grid_shows_every_open_shift_and_labels_the_unreleased(app, client, venue):
    day = _next_week_wednesday()
    unreleased_id = _open_shift(app, venue["id"], day)
    _hide_unnotified(app, venue)

    login_as_pub(client, venue["pub_id"])
    grid = client.get(f"/v/{venue['slug']}/rota/?week={day.isoformat()}").data
    assert b"18:35-22:25" in grid
    assert LABEL in grid
    assert LABEL in client.get(f"/v/{venue['slug']}/rota/open-shift/day/{day.isoformat()}").data
    assert LABEL in client.get(f"/v/{venue['slug']}/rota/open-shift/{unreleased_id}").data

    # Once its alert has gone out the label goes, on all three screens.
    client.post(f"/v/{venue['slug']}/rota/shift/{unreleased_id}/notify")
    grid = client.get(f"/v/{venue['slug']}/rota/?week={day.isoformat()}").data
    assert b"18:35-22:25" in grid
    assert LABEL not in grid
    assert LABEL not in client.get(f"/v/{venue['slug']}/rota/open-shift/day/{day.isoformat()}").data
    assert LABEL not in client.get(f"/v/{venue['slug']}/rota/open-shift/{unreleased_id}").data


def test_admin_grid_has_no_label_while_the_setting_is_ticked(app, client, venue):
    day = _next_week_wednesday()
    _open_shift(app, venue["id"], day)

    login_as_pub(client, venue["pub_id"])
    grid = client.get(f"/v/{venue['slug']}/rota/?week={day.isoformat()}").data
    assert b"18:35-22:25" in grid
    assert LABEL not in grid


def test_settings_hint_says_open_shifts_are_held_back(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    page = client.get(f"/v/{venue['slug']}/admin/settings").data
    assert b"Open shifts in that week are held back too" in page
    assert b"can still be seen and claimed" not in page


# ---------- Claiming needs the shift's job role (same change, 12 November 2026) ----------


def _role(app, venue_id, name):
    with app.app_context():
        conn = db_module.get_db()
        role_id = conn.execute("INSERT INTO venue_role (venue_id, name) VALUES (?, ?)", (venue_id, name)).lastrowid
        conn.commit()
        return role_id


def _give_role(app, shift_id, role_id):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE shift SET venue_role_id = ? WHERE id = ?", (role_id, shift_id))
        conn.commit()


def test_matching_job_role_can_claim_a_role_specific_shift(app, client, venue):
    kitchen = _role(app, venue["id"], "Kitchen")
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Kitchen Kim", role_id=kitchen)
    shift_id = _open_shift(app, venue["id"], _next_week_wednesday())
    _give_role(app, shift_id, kitchen)

    login_as_person(client, person_id)
    listing = client.get(f"/v/{venue['slug']}/staff/open-shifts").data
    assert f"/open-shifts/{shift_id}/claim".encode() in listing
    assert client.get(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}").status_code == 200
    client.post(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}/claim")
    assert _shift_state(app, shift_id) == ("scheduled", person_id)


def test_another_job_role_sees_the_shift_but_cannot_claim_it(app, client, venue):
    kitchen = _role(app, venue["id"], "Kitchen")
    bar = _role(app, venue["id"], "Front bar")
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Bar Bea", role_id=bar)
    shift_id = _open_shift(app, venue["id"], _next_week_wednesday())
    _give_role(app, shift_id, kitchen)

    login_as_person(client, person_id)
    listing = client.get(f"/v/{venue['slug']}/staff/open-shifts").data
    assert b"18:35" in listing and b"Kitchen only" in listing
    assert f"/open-shifts/{shift_id}/claim".encode() not in listing

    prompt = client.get(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}", follow_redirects=True)
    assert b"That shift is for Kitchen only" in prompt.data
    claim = client.post(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}/claim", follow_redirects=True)
    assert b"That shift is for Kitchen only" in claim.data
    assert _shift_state(app, shift_id) == ("open", None)


def test_someone_with_no_job_role_cannot_claim_a_role_specific_shift(app, client, venue):
    kitchen = _role(app, venue["id"], "Kitchen")
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Roleless Ray")
    shift_id = _open_shift(app, venue["id"], _next_week_wednesday())
    _give_role(app, shift_id, kitchen)

    login_as_person(client, person_id)
    client.post(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}/claim")
    assert _shift_state(app, shift_id) == ("open", None)


def test_a_shift_with_no_role_is_open_to_any_job_role(app, client, venue):
    bar = _role(app, venue["id"], "Front bar")
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Any Ann", role_id=bar)
    shift_id = _open_shift(app, venue["id"], _next_week_wednesday())

    login_as_person(client, person_id)
    client.post(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}/claim")
    assert _shift_state(app, shift_id) == ("scheduled", person_id)


def test_a_manager_can_still_assign_a_role_specific_shift_to_anyone_on_the_grid(app, client, venue):
    kitchen = _role(app, venue["id"], "Kitchen")
    bar = _role(app, venue["id"], "Front bar")
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Bar Bea", role_id=bar)
    day = _next_week_wednesday()
    shift_id = _open_shift(app, venue["id"], day)
    _give_role(app, shift_id, kitchen)

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/rota/shift/{shift_id}/move",
                       json={"person_id": person_id, "shift_date": day.isoformat()})
    assert resp.status_code == 200
    assert _shift_state(app, shift_id) == ("scheduled", person_id)
