import os

from app import db as db_module
from tests.conftest import create_active_staff


def test_clock_status_requires_bearer_auth(client):
    resp = client.post("/internal/clock-status", json={"pub_id": 1, "person_id": 1})
    assert resp.status_code == 401


def test_clock_status_returns_false_when_not_clocked_in(app, client, venue, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    person_id, _m, _e = create_active_staff(app, venue["id"])

    resp = client.post(
        "/internal/clock-status",
        json={"pub_id": venue["pub_id"], "person_id": person_id},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["clocked_in"] is False


def test_clock_status_returns_true_when_currently_clocked_in(app, client, venue, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Clocked In Person")

    with app.app_context():
        conn = db_module.get_db()
        shift_cur = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, '2026-01-01', '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id),
        )
        shift_id = shift_cur.lastrowid
        conn.execute("INSERT INTO attendance (shift_id, clock_in_at) VALUES (?, datetime('now'))", (shift_id,))
        conn.commit()

    resp = client.post(
        "/internal/clock-status",
        json={"pub_id": venue["pub_id"], "person_id": person_id},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["clocked_in"] is True
    assert body["name"] == "Clocked In Person"


def test_clock_status_unknown_pub_id_returns_false(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    resp = client.post(
        "/internal/clock-status",
        json={"pub_id": 999999, "person_id": 1},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["clocked_in"] is False
