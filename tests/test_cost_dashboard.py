from datetime import date, timedelta

from app import db as db_module
from app.costs import actual_cost, predicted_cost
from tests.conftest import create_active_staff, login_as_pub


def test_predicted_cost_from_scheduled_shift(app, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"])
    today = date.today().isoformat()
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id, today),
        )
        conn.commit()
        # 8 hours * £12.50 = £100
        assert predicted_cost(venue["id"], today, today) == 100.0


def test_actual_cost_from_attendance(app, venue):
    person_id, _m, _e = create_active_staff(app, venue["id"])
    today = date.today().isoformat()
    with app.app_context():
        conn = db_module.get_db()
        shift_cur = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id, today),
        )
        shift_id = shift_cur.lastrowid
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at) VALUES (?, ?, ?)",
            (shift_id, f"{today}T09:00:00", f"{today}T13:00:00"),
        )
        conn.commit()
        # 4 actual hours * £12.50 = £50, even though the plan was 8 hours
        assert actual_cost(venue["id"], today, today) == 50.0


def test_dashboard_week_renders_with_no_turnover_entered(app, client, venue):
    """Regression coverage: the venue fixture sets target_staff_cost_percent
    without any weekly_turnover row — the dashboard must render cleanly
    (percentages as '—') rather than crash dividing by a missing turnover
    figure."""
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/dashboard/")
    assert resp.status_code == 200
    assert b"\xe2\x80\x94" in resp.data  # the em-dash placeholder for an unset %


def test_weekly_turnover_can_be_entered_and_shows_percent_used(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"])
    monday = date.today() - timedelta(days=date.today().weekday())
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id, monday.isoformat()),
        )
        conn.commit()

    client.post(
        f"/v/{venue['slug']}/dashboard/turnover",
        data={"week_start_date": monday.isoformat(), "predicted_amount": "1000"},
    )
    resp = client.get(f"/v/{venue['slug']}/dashboard/?week={monday.isoformat()}")
    assert resp.status_code == 200
    # £100 predicted staff cost (8hrs * £12.50) / £1000 predicted turnover = 10.0%
    assert b"10.0%" in resp.data


def test_weekly_turnover_upsert_preserves_the_other_field(app, client, venue):
    """Setting predicted turnover shouldn't wipe out an already-entered
    actual figure, and vice versa — same COALESCE-on-conflict behaviour the
    old daily table had."""
    login_as_pub(client, venue["pub_id"])
    monday = date.today() - timedelta(days=date.today().weekday())

    client.post(
        f"/v/{venue['slug']}/dashboard/turnover",
        data={"week_start_date": monday.isoformat(), "predicted_amount": "1000"},
    )
    client.post(
        f"/v/{venue['slug']}/dashboard/turnover",
        data={"week_start_date": monday.isoformat(), "actual_amount": "1200"},
    )

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT * FROM weekly_turnover WHERE venue_id = ? AND week_start_date = ?",
            (venue["id"], monday.isoformat()),
        ).fetchone()
        assert row["predicted_amount"] == 1000
        assert row["actual_amount"] == 1200


def test_month_view_groups_weeks_by_the_month_their_monday_falls_in(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    # 2026-08-31 is a Monday; its week runs into September, but it belongs to August.
    client.post(
        f"/v/{venue['slug']}/dashboard/turnover",
        data={"week_start_date": "2026-08-31", "predicted_amount": "500"},
    )
    resp = client.get(f"/v/{venue['slug']}/dashboard/month?month=2026-08")
    assert resp.status_code == 200
    assert b"500.00" in resp.data

    resp_sept = client.get(f"/v/{venue['slug']}/dashboard/month?month=2026-09")
    assert b"500.00" not in resp_sept.data


def test_month_view_totals_sum_across_weeks(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    for week_start in ("2026-08-03", "2026-08-10"):
        client.post(
            f"/v/{venue['slug']}/dashboard/turnover",
            data={"week_start_date": week_start, "predicted_amount": "1000"},
        )
    resp = client.get(f"/v/{venue['slug']}/dashboard/month?month=2026-08")
    assert resp.status_code == 200
    assert b"2000.00" in resp.data  # 1000 + 1000 across the two weeks entered


def test_admin_can_set_a_start_date(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    _person_id, membership_id, _email = create_active_staff(app, venue["id"], name="Started Today")

    client.post(
        f"/v/{venue['slug']}/admin/staff/{membership_id}/edit",
        data={"name": "Started Today", "start_date": "2024-03-15"},
    )

    with app.app_context():
        conn = db_module.get_db()
        detail = conn.execute(
            "SELECT start_date FROM rota_staff_detail WHERE venue_membership_id = ?", (membership_id,)
        ).fetchone()
    assert detail["start_date"] == "2024-03-15"


def test_birthdays_dashboard_shows_an_upcoming_birthday(app, client, venue):
    person_id, _membership_id, _email = create_active_staff(app, venue["id"], name="Birthday Person")
    upcoming_dob = date.today() + timedelta(days=5)
    # Year doesn't matter for a birthday, just month/day - use a year safely in the past.
    dob = date(1990, upcoming_dob.month, upcoming_dob.day)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE person SET date_of_birth = ? WHERE id = ?", (dob.isoformat(), person_id))
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/dashboard/birthdays")
    assert resp.status_code == 200
    assert b"Birthday Person" in resp.data
    assert "🎂".encode() in resp.data
    assert b"birthday in 5 day" in resp.data


def test_birthdays_dashboard_shows_an_upcoming_anniversary_with_correct_year_count(app, client, venue):
    person_id, membership_id, _email = create_active_staff(app, venue["id"], name="Anniversary Person")
    upcoming = date.today() + timedelta(days=7)
    start_date = date(upcoming.year - 3, upcoming.month, upcoming.day)  # 3 years ago from the upcoming date
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_staff_detail SET start_date = ? WHERE venue_membership_id = ?",
            (start_date.isoformat(), membership_id),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/dashboard/birthdays")
    assert resp.status_code == 200
    assert b"Anniversary Person" in resp.data
    assert "🎉".encode() in resp.data
    assert b"3 year anniversary in 7 day" in resp.data


def test_birthdays_dashboard_skips_a_future_start_date(app, client, venue):
    """A start date that's still in the future (not yet started) must not
    show a nonsensical '0 year anniversary' — there's no such thing before
    anyone's actually started."""
    person_id, membership_id, _email = create_active_staff(app, venue["id"], name="Not Started Yet")
    start_date = date.today() + timedelta(days=20)  # month/day hasn't occurred yet this year
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_staff_detail SET start_date = ? WHERE venue_membership_id = ?",
            (start_date.isoformat(), membership_id),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/dashboard/birthdays")
    assert resp.status_code == 200
    assert b"Not Started Yet" not in resp.data


def test_birthdays_dashboard_shows_first_anniversary_for_a_recent_start(app, client, venue):
    """Someone who started a few months ago should show their upcoming
    FIRST anniversary (1 year from their start date), not be skipped."""
    person_id, membership_id, _email = create_active_staff(app, venue["id"], name="Recent Starter")
    start_date = date.today() - timedelta(days=90)  # month/day already passed this year
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_staff_detail SET start_date = ? WHERE venue_membership_id = ?",
            (start_date.isoformat(), membership_id),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/dashboard/birthdays")
    assert resp.status_code == 200
    assert b"Recent Starter" in resp.data
    assert b"1 year anniversary" in resp.data


def test_birthdays_dashboard_shows_both_kinds_for_the_same_person(app, client, venue):
    person_id, membership_id, _email = create_active_staff(app, venue["id"], name="Double Date")
    soon = date.today() + timedelta(days=3)
    dob = date(1985, soon.month, soon.day)
    start_date = date(soon.year - 2, soon.month, soon.day)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE person SET date_of_birth = ? WHERE id = ?", (dob.isoformat(), person_id))
        conn.execute(
            "UPDATE rota_staff_detail SET start_date = ? WHERE venue_membership_id = ?",
            (start_date.isoformat(), membership_id),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/dashboard/birthdays")
    assert resp.data.count(b"Double Date") == 2  # one birthday entry, one anniversary entry
