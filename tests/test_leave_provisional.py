"""
"These figures aren't final yet."

Every pub that signs up does so partway through its holiday year, with staff
who have already taken leave the app knows nothing about. Until that history
is entered (rota_grid.leave_history) every balance is too generous — and a
staff member who books time off against a figure we showed them has been
misled by us, not by their manager.

A message sent once is read once; the wrong number sits there for weeks. So
this is a venue setting that puts the caveat in the same place as the figure,
for as long as it is true, and it travels with the exported reports because a
PDF of holiday balances is exactly the thing that gets sent on to somebody
else.
"""

from app import db as db_module
from tests.conftest import create_active_staff, login_as_person, login_as_pub

MON_TO_FRI = '{"mon":true,"tue":true,"wed":true,"thu":true,"fri":true,"sat":false,"sun":false}'
# Literal template text, so Jinja does not escape the apostrophe (autoescaping
# only touches variables).
NOTICE = "aren't final yet"


def _staff(app, venue, name="Provisional Person"):
    person_id, membership_id, _e = create_active_staff(app, venue["id"], name=name)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE rota_staff_detail SET availability = ? WHERE venue_membership_id = ?",
                     (MON_TO_FRI, membership_id))
        conn.commit()
    return person_id, membership_id


def _set_provisional(app, venue, on=True):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE venue_settings SET leave_figures_provisional = ? WHERE venue_id = ?",
                     (1 if on else 0, venue["id"]))
        conn.commit()


def _staff_leave_page(client, venue, person_id):
    login_as_person(client, person_id)
    return client.get(f"/v/{venue['slug']}/staff/leave").data.decode("utf-8")


# --------------------------------------------------------------------------- #
# What the staff member sees
# --------------------------------------------------------------------------- #

def test_off_by_default_so_an_established_pub_sees_nothing(app, client, venue):
    """A pub with no history to enter must not be told its figures are wrong."""
    person_id, _m = _staff(app, venue)

    assert NOTICE not in _staff_leave_page(client, venue, person_id)


def test_the_staff_leave_page_says_so_when_it_is_set(app, client, venue):
    person_id, _m = _staff(app, venue)
    _set_provisional(app, venue)

    html = _staff_leave_page(client, venue, person_id)

    assert NOTICE in html
    assert "check with your manager before booking" in html


def test_the_notice_comes_above_the_figures(app, client, venue):
    """Somebody who reads the number first and the caveat second has already
    believed the number."""
    person_id, _m = _staff(app, venue)
    _set_provisional(app, venue)

    html = _staff_leave_page(client, venue, person_id)

    assert html.index(NOTICE) < html.index("Left to book")


def test_turning_it_off_removes_it(app, client, venue):
    person_id, _m = _staff(app, venue)
    _set_provisional(app, venue)
    _set_provisional(app, venue, on=False)

    assert NOTICE not in _staff_leave_page(client, venue, person_id)


def test_requesting_leave_still_works_while_it_is_set(app, client, venue):
    """It is a caveat, not a lock. The rota carries on as normal."""
    person_id, _m = _staff(app, venue)
    _set_provisional(app, venue)
    login_as_person(client, person_id)

    client.post(f"/v/{venue['slug']}/staff/leave",
                data={"start_date": "2027-02-01", "end_date": "2027-02-05", "leave_type": "paid"},
                follow_redirects=True)

    with app.app_context():
        count = db_module.get_db().execute(
            "SELECT COUNT(*) AS n FROM leave_request WHERE person_id = ?", (person_id,)
        ).fetchone()["n"]
    assert count == 1


# --------------------------------------------------------------------------- #
# The admin side
# --------------------------------------------------------------------------- #

def test_the_setting_can_be_ticked_and_unticked_on_the_settings_screen(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    base = {"venue_name": "Test Venue", "pay_period_type": "weekly",
            "holiday_year_start_day": "1", "holiday_year_start_month": "1"}

    client.post(f"/v/{venue['slug']}/admin/settings",
                data={**base, "leave_figures_provisional": "on"}, follow_redirects=True)
    with app.app_context():
        on = db_module.get_db().execute(
            "SELECT leave_figures_provisional FROM venue_settings WHERE venue_id = ?", (venue["id"],)
        ).fetchone()["leave_figures_provisional"]

    client.post(f"/v/{venue['slug']}/admin/settings", data=base, follow_redirects=True)
    with app.app_context():
        off = db_module.get_db().execute(
            "SELECT leave_figures_provisional FROM venue_settings WHERE venue_id = ?", (venue["id"],)
        ).fetchone()["leave_figures_provisional"]

    assert on == 1 and off == 0


def test_the_admin_leave_report_carries_the_warning_too(app, client, venue):
    _staff(app, venue)
    _set_provisional(app, venue)
    login_as_pub(client, venue["pub_id"])

    html = client.get(f"/v/{venue['slug']}/leave/").data.decode("utf-8")

    assert NOTICE in html


def test_the_csv_export_says_it_is_not_final(app, client, venue):
    """The export is the thing that gets sent on to somebody else."""
    _staff(app, venue)
    _set_provisional(app, venue)
    login_as_pub(client, venue["pub_id"])

    text = client.get(f"/v/{venue['slug']}/leave/export.csv").data.decode("utf-8")

    assert "NOT FINAL" in text
    text.encode("ascii")  # Excel opens a BOM-less UTF-8 CSV as Windows-1252


def test_the_csv_export_is_clean_when_the_figures_are_final(app, client, venue):
    _staff(app, venue)
    login_as_pub(client, venue["pub_id"])

    text = client.get(f"/v/{venue['slug']}/leave/export.csv").data.decode("utf-8")

    assert "NOT FINAL" not in text


def test_the_pdf_export_builds_with_the_warning(app, client, venue):
    _staff(app, venue)
    _set_provisional(app, venue)
    login_as_pub(client, venue["pub_id"])

    resp = client.get(f"/v/{venue['slug']}/leave/export.pdf")

    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF")
