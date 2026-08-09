from datetime import datetime, timedelta

from app import db as db_module
from tests.conftest import create_active_staff


def _make_scheduled_shift(app, venue_id, person_id, shift_date, start_time, end_time):
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, ?, ?, 'scheduled')",
            (venue_id, person_id, shift_date, start_time, end_time),
        )
        conn.commit()
        return cur.lastrowid


def _enable_notification(app, venue, notification_type, recipient_person_id):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO notification_setting (venue_id, notification_type, enabled, method) VALUES (?, ?, 1, 'email')",
            (venue["id"], notification_type),
        )
        setting_id = conn.execute(
            "SELECT id FROM notification_setting WHERE venue_id = ? AND notification_type = ?",
            (venue["id"], notification_type),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO notification_recipient (notification_setting_id, person_id) VALUES (?, ?)",
            (setting_id, recipient_person_id),
        )
        conn.commit()


def test_run_shift_notifications_requires_bearer_auth(client):
    resp = client.post("/internal/run-shift-notifications")
    assert resp.status_code == 401


def test_run_shift_notifications_reports_counts_and_notifies(app, client, venue, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    sent = []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: sent.append(a) or True)
    _enable_notification(app, venue, "missed_clock_in", venue["owner_person_id"])

    person_id, _m, _e = create_active_staff(app, venue["id"], name="Late Arriver")
    shift_start = datetime.now() - timedelta(minutes=20)  # past the 15 min grace, real "now"
    _make_scheduled_shift(app, venue["id"], person_id, shift_start.date().isoformat(),
                           shift_start.strftime("%H:%M"), "23:59")

    resp = client.post(
        "/internal/run-shift-notifications",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"missed_clock_in": 1, "missed_clock_out": 0}
    assert len(sent) == 1


def test_run_shift_notifications_second_call_does_not_resend(app, client, venue, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    sent = []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: sent.append(a) or True)
    _enable_notification(app, venue, "missed_clock_in", venue["owner_person_id"])

    person_id, _m, _e = create_active_staff(app, venue["id"], name="Checked Twice")
    shift_start = datetime.now() - timedelta(minutes=20)
    _make_scheduled_shift(app, venue["id"], person_id, shift_start.date().isoformat(),
                           shift_start.strftime("%H:%M"), "23:59")

    headers = {"Authorization": "Bearer test-secret"}
    first = client.post("/internal/run-shift-notifications", headers=headers)
    second = client.post("/internal/run-shift-notifications", headers=headers)

    assert first.get_json()["missed_clock_in"] == 1
    assert second.get_json()["missed_clock_in"] == 0
    assert len(sent) == 1
