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
