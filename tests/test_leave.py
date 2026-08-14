from datetime import date, timedelta

from app import db as db_module
from app.leave import days_taken_count, normalize_holiday_year_start
from tests.conftest import create_active_staff, login_as_person, login_as_pub


def test_normalize_holiday_year_start_accepts_common_formats():
    assert normalize_holiday_year_start("01-01") == "01-01"
    assert normalize_holiday_year_start("0101") == "01-01"
    assert normalize_holiday_year_start("01/01") == "01-01"
    assert normalize_holiday_year_start("01.01") == "01-01"
    assert normalize_holiday_year_start("1 1") == "01-01"
    assert normalize_holiday_year_start("4-6") == "04-06"
    assert normalize_holiday_year_start("02-29") == "02-29"  # validated against a leap year


def test_normalize_holiday_year_start_rejects_ambiguous_and_invalid_input():
    assert normalize_holiday_year_start("") is None
    assert normalize_holiday_year_start("101") is None  # ambiguous: 1st-01 or 10th-1?
    assert normalize_holiday_year_start("not a date") is None
    assert normalize_holiday_year_start("13-01") is None  # no month 13
    assert normalize_holiday_year_start("02-30") is None  # no 30 Feb


def test_staff_can_request_leave_and_admin_can_approve(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"])
    login_as_person(client, person_id)

    start = date.today().isoformat()
    end = (date.today() + timedelta(days=2)).isoformat()
    client.post(f"/v/{venue['slug']}/staff/leave", data={"start_date": start, "end_date": end})

    with app.app_context():
        conn = db_module.get_db()
        leave_row = conn.execute("SELECT * FROM leave_request WHERE person_id = ?", (person_id,)).fetchone()
        assert leave_row["status"] == "pending"
        leave_id = leave_row["id"]

    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/rota/leave/{leave_id}/approve")

    with app.app_context():
        conn = db_module.get_db()
        leave_row = conn.execute("SELECT status FROM leave_request WHERE id = ?", (leave_id,)).fetchone()
        assert leave_row["status"] == "approved"


def test_days_taken_only_counts_normal_working_days():
    class FakeConn:
        def execute(self, _query, _params):
            class Result:
                def fetchall(self_inner):
                    return [{"start_date": "2026-01-05", "end_date": "2026-01-11"}]  # Mon 5th - Sun 11th

            return Result()

    availability = '{"mon":true,"tue":true,"wed":true,"thu":true,"fri":true,"sat":false,"sun":false}'
    count = days_taken_count(FakeConn(), person_id=1, availability_json=availability, year_start_mmdd="01-01", today=date(2026, 1, 12))
    # Mon-Fri = 5 working days counted; Sat/Sun don't count since never worked.
    assert count == 5


def test_days_taken_falls_back_to_jan_1_for_a_malformed_year_start(app):
    """Real production crash: a venue's holiday_year_start_date was saved as
    '0101' (no dash) before save-time validation existed (see
    admin_config.py's settings route) -- splitting that on '-' yields a
    single value, and unpacking it into month, day used to raise
    ValueError, taking down every staff member's leave page at that venue."""
    class FakeConn:
        def execute(self, _query, _params):
            class Result:
                def fetchall(self_inner):
                    return [{"start_date": "2026-01-05", "end_date": "2026-01-11"}]

            return Result()

    availability = '{"mon":true,"tue":true,"wed":true,"thu":true,"fri":true,"sat":false,"sun":false}'
    count = days_taken_count(
        FakeConn(), person_id=1, availability_json=availability,
        year_start_mmdd="0101", today=date(2026, 1, 12),
    )
    assert count == 5  # same result as a clean "01-01" would give, via the 1 Jan fallback


def test_approved_leave_shows_palm_tree_on_grid(app, client, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"])
    today = date.today().isoformat()

    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO leave_request (person_id, venue_id, start_date, end_date, status) VALUES (?, ?, ?, ?, 'approved')",
            (person_id, venue["id"], today, today),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/rota/")
    assert b"cell-leave" in resp.data


def test_admin_can_add_leave_directly_and_it_is_immediately_approved(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="AdminAdded")

    resp = client.post(
        f"/v/{venue['slug']}/rota/leave/create",
        data={"person_id": person_id, "start_date": "2026-08-03", "end_date": "2026-08-05"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Leave added" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT * FROM leave_request WHERE person_id = ?", (person_id,)).fetchone()
        assert row is not None
        assert row["status"] == "approved"  # no pending-queue step, unlike a staff self-request
        assert row["start_date"] == "2026-08-03"
        assert row["end_date"] == "2026-08-05"

    # It shows up on the grid straight away, same as any other approved leave.
    resp = client.get(f"/v/{venue['slug']}/rota/?week=2026-08-03")
    assert b"cell-leave" in resp.data


def test_admin_add_leave_rejects_invalid_date_range(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="BadRange")

    resp = client.post(
        f"/v/{venue['slug']}/rota/leave/create",
        data={"person_id": person_id, "start_date": "2026-08-05", "end_date": "2026-08-03"},  # end before start
        follow_redirects=True,
    )
    assert b"valid date range" in resp.data.lower()

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT 1 FROM leave_request WHERE person_id = ?", (person_id,)).fetchone() is None


def test_admin_add_leave_rejects_person_not_at_this_venue(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/rota/leave/create",
        data={"person_id": 999999, "start_date": "2026-08-03", "end_date": "2026-08-05"},
        follow_redirects=True,
    )
    assert b"valid date range" in resp.data.lower()

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT 1 FROM leave_request WHERE person_id = 999999").fetchone() is None


def test_admin_add_leave_requires_admin_permission(client, venue):
    resp = client.post(
        f"/v/{venue['slug']}/rota/leave/create",
        data={"person_id": 1, "start_date": "2026-08-03", "end_date": "2026-08-05"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
