"""
Leave switched off for one person.

Customer request, 2026-10-01: most of a pub's staff are contracted and use
leave, but the ones who just help out from time to time have none to book, and
seeing a Leave page made them ask about it. The landlord ticks "Leave doesn't
apply to them" on the staff record, and that person:

  * has no Leave button and no Leave page, and cannot post a request;
  * is left out of holiday balances on the Leave report;
  * is not part of "Add leave for everyone".

It hides the feature from the person. It does not handcuff the landlord, who
can still record leave for them by hand, and what is recorded is still reported.
"""

from app import db as db_module
from tests.conftest import create_active_staff, login_as_person, login_as_pub


def _staff(app, venue, name="Casual Helper", leave_off=1):
    person_id, membership_id, _e = create_active_staff(app, venue["id"], name=name)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE rota_staff_detail SET leave_off = ? WHERE venue_membership_id = ?",
                     (leave_off, membership_id))
        conn.commit()
    return person_id, membership_id


def _detail(app, membership_id):
    with app.app_context():
        return db_module.get_db().execute(
            "SELECT * FROM rota_staff_detail WHERE venue_membership_id = ?", (membership_id,)).fetchone()


def _leave_count(app, person_id):
    with app.app_context():
        return db_module.get_db().execute(
            "SELECT COUNT(*) AS n FROM leave_request WHERE person_id = ?", (person_id,)).fetchone()["n"]


# --------------------------------------------------------------------------- #
# What the staff member sees
# --------------------------------------------------------------------------- #

def test_leave_is_on_for_everybody_until_it_is_switched_off(app, client, venue):
    person_id, _m = _staff(app, venue, leave_off=0)
    login_as_person(client, person_id)

    assert f"/v/{venue['slug']}/staff/leave".encode() in client.get(f"/v/{venue['slug']}/staff/").data
    assert client.get(f"/v/{venue['slug']}/staff/leave").status_code == 200


def test_the_leave_button_is_gone_from_my_shifts(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_person(client, person_id)

    resp = client.get(f"/v/{venue['slug']}/staff/")

    assert resp.status_code == 200
    assert f"/v/{venue['slug']}/staff/leave".encode() not in resp.data
    assert b"Full rota" in resp.data  # the rest of the row is still there


def test_the_leave_page_itself_sends_them_back_to_their_shifts(app, client, venue):
    """A bookmark or a typed address must not show an allowance the button was
    removed to hide."""
    person_id, _m = _staff(app, venue)
    login_as_person(client, person_id)

    resp = client.get(f"/v/{venue['slug']}/staff/leave", follow_redirects=True)

    assert b"My upcoming shifts" in resp.data
    assert b"Your allowance" not in resp.data
    assert b"Request leave" not in resp.data


def test_a_posted_request_is_refused(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_person(client, person_id)

    client.post(f"/v/{venue['slug']}/staff/leave",
                data={"start_date": "2027-03-01", "end_date": "2027-03-02", "leave_type": "paid"})

    assert _leave_count(app, person_id) == 0


# --------------------------------------------------------------------------- #
# The switch on the staff record
# --------------------------------------------------------------------------- #

def test_ticking_the_box_switches_leave_off_and_unticking_brings_it_back(app, client, venue):
    _person_id, membership_id = _staff(app, venue, leave_off=0)
    login_as_pub(client, venue["pub_id"])
    url = f"/v/{venue['slug']}/admin/staff/{membership_id}/edit"

    client.post(url, data={"name": "Casual Helper", "leave_off": "on"})
    assert _detail(app, membership_id)["leave_off"] == 1

    client.post(url, data={"name": "Casual Helper"})
    assert _detail(app, membership_id)["leave_off"] == 0


def test_the_staff_record_hides_the_holiday_figures_but_keeps_them(app, client, venue):
    """Switching leave off and saving must not wipe an allowance somebody typed:
    unticking it later should bring back what was there."""
    _person_id, membership_id = _staff(app, venue)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE rota_staff_detail SET allowance_days = 12 WHERE venue_membership_id = ?",
                     (membership_id,))
        conn.commit()
    login_as_pub(client, venue["pub_id"])

    html = client.get(f"/v/{venue['slug']}/admin/staff/{membership_id}/edit").data.decode("utf-8")

    assert 'id="leave_off" name="leave_off" checked' in html
    assert "Holiday allowance (days)" not in html
    assert 'type="hidden" name="allowance_days" value="12"' in html


# --------------------------------------------------------------------------- #
# Everyone-at-once, and the report
# --------------------------------------------------------------------------- #

def test_leave_for_everyone_leaves_them_out(app, client, venue):
    casual, _m = _staff(app, venue, name="Casual Helper")
    contracted, _m2 = _staff(app, venue, name="Contracted Person", leave_off=0)
    login_as_pub(client, venue["pub_id"])

    client.post(f"/v/{venue['slug']}/rota/leave/bulk",
                data={"start_date": "2027-01-04", "end_date": "2027-01-08", "leave_type": "paid"})

    assert _leave_count(app, casual) == 0
    assert _leave_count(app, contracted) == 1


def test_the_leave_report_has_no_balance_for_them(app, client, venue):
    _staff(app, venue, name="Casual Helper")
    _staff(app, venue, name="Contracted Person", leave_off=0)
    login_as_pub(client, venue["pub_id"])

    html = client.get(f"/v/{venue['slug']}/leave/").data.decode("utf-8")

    assert "Contracted Person" in html
    assert "Casual Helper" not in html


def test_an_admin_can_still_record_leave_for_them_and_it_is_reported(app, client, venue):
    person_id, _m = _staff(app, venue, name="Casual Helper")
    login_as_pub(client, venue["pub_id"])

    client.post(f"/v/{venue['slug']}/rota/leave/create",
                data={"person_id": person_id, "start_date": "2027-02-01", "end_date": "2027-02-02",
                      "leave_type": "unpaid"})
    assert _leave_count(app, person_id) == 1

    html = client.get(f"/v/{venue['slug']}/leave/?start=2027-01-01&end=2027-12-31").data.decode("utf-8")
    taken, position = html.split("Holiday position this year")
    assert "Casual Helper" in taken          # the leave happened
    assert "Casual Helper" not in position   # but there is still no balance
