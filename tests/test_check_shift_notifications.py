from datetime import date, datetime, timedelta

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


def test_missed_clock_in_detected_after_grace_period(app, venue, monkeypatch):
    from scripts.check_shift_notifications import check_missed_clock_ins

    sent = []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: sent.append(a) or True)
    _enable_notification(app, venue, "missed_clock_in", venue["owner_person_id"])

    person_id, _m, _e = create_active_staff(app, venue["id"], name="Late Arriver")
    today = date.today()
    shift_start = datetime.combine(today, datetime.min.time()) + timedelta(hours=9)  # 09:00 today
    _make_scheduled_shift(app, venue["id"], person_id, today.isoformat(), "09:00", "17:00")

    now = shift_start + timedelta(minutes=20)  # 20 min late - past the 15 min grace
    with app.app_context():
        conn = db_module.get_db()
        count = check_missed_clock_ins(conn, now)

    assert count == 1
    assert len(sent) == 1
    assert "Late Arriver" in sent[0][2]


def test_missed_clock_in_not_flagged_within_grace_period(app, venue, monkeypatch):
    from scripts.check_shift_notifications import check_missed_clock_ins

    sent = []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: sent.append(a) or True)
    _enable_notification(app, venue, "missed_clock_in", venue["owner_person_id"])

    person_id, _m, _e = create_active_staff(app, venue["id"], name="On Time Ish")
    today = date.today()
    shift_start = datetime.combine(today, datetime.min.time()) + timedelta(hours=9)
    _make_scheduled_shift(app, venue["id"], person_id, today.isoformat(), "09:00", "17:00")

    now = shift_start + timedelta(minutes=5)  # only 5 min late - still within grace
    with app.app_context():
        conn = db_module.get_db()
        count = check_missed_clock_ins(conn, now)

    assert count == 0
    assert sent == []


def test_missed_clock_in_not_flagged_once_clocked_in(app, venue, monkeypatch):
    from scripts.check_shift_notifications import check_missed_clock_ins

    sent = []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: sent.append(a) or True)
    _enable_notification(app, venue, "missed_clock_in", venue["owner_person_id"])

    person_id, _m, _e = create_active_staff(app, venue["id"], name="Clocked In")
    today = date.today()
    shift_start = datetime.combine(today, datetime.min.time()) + timedelta(hours=9)
    shift_id = _make_scheduled_shift(app, venue["id"], person_id, today.isoformat(), "09:00", "17:00")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at) VALUES (?, ?)",
            (shift_id, datetime.now().isoformat()),
        )
        conn.commit()

    now = shift_start + timedelta(minutes=20)
    with app.app_context():
        conn = db_module.get_db()
        count = check_missed_clock_ins(conn, now)

    assert count == 0
    assert sent == []


def test_missed_clock_in_only_notified_once_across_repeated_runs(app, venue, monkeypatch):
    from scripts.check_shift_notifications import check_missed_clock_ins

    sent = []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: sent.append(a) or True)
    _enable_notification(app, venue, "missed_clock_in", venue["owner_person_id"])

    person_id, _m, _e = create_active_staff(app, venue["id"], name="Checked Twice")
    today = date.today()
    shift_start = datetime.combine(today, datetime.min.time()) + timedelta(hours=9)
    _make_scheduled_shift(app, venue["id"], person_id, today.isoformat(), "09:00", "17:00")

    now = shift_start + timedelta(minutes=20)
    with app.app_context():
        conn = db_module.get_db()
        first_count = check_missed_clock_ins(conn, now)
        second_count = check_missed_clock_ins(conn, now + timedelta(minutes=5))  # simulated next cron run

    assert first_count == 1
    assert second_count == 0  # already considered, not re-sent
    assert len(sent) == 1


def test_missed_clock_out_detected_after_grace_period(app, venue, monkeypatch):
    from scripts.check_shift_notifications import check_missed_clock_outs

    sent = []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: sent.append(a) or True)
    _enable_notification(app, venue, "missed_clock_out", venue["owner_person_id"])

    person_id, _m, _e = create_active_staff(app, venue["id"], name="Forgot To Leave")
    today = date.today()
    shift_end = datetime.combine(today, datetime.min.time()) + timedelta(hours=17)
    shift_id = _make_scheduled_shift(app, venue["id"], person_id, today.isoformat(), "09:00", "17:00")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("INSERT INTO attendance (shift_id, clock_in_at) VALUES (?, ?)", (shift_id, datetime.now().isoformat()))
        conn.commit()

    now = shift_end + timedelta(minutes=20)
    with app.app_context():
        conn = db_module.get_db()
        count = check_missed_clock_outs(conn, now)

    assert count == 1
    assert "Forgot To Leave" in sent[0][2]


def test_missed_clock_out_not_flagged_if_never_clocked_in(app, venue, monkeypatch):
    """Someone who never clocked in at all is a missed-clock-IN case, not a
    missed-clock-out - must not double-fire both."""
    from scripts.check_shift_notifications import check_missed_clock_outs

    sent = []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: sent.append(a) or True)
    _enable_notification(app, venue, "missed_clock_out", venue["owner_person_id"])

    person_id, _m, _e = create_active_staff(app, venue["id"], name="Never Showed")
    today = date.today()
    shift_end = datetime.combine(today, datetime.min.time()) + timedelta(hours=17)
    _make_scheduled_shift(app, venue["id"], person_id, today.isoformat(), "09:00", "17:00")

    now = shift_end + timedelta(minutes=20)
    with app.app_context():
        conn = db_module.get_db()
        count = check_missed_clock_outs(conn, now)

    assert count == 0
    assert sent == []


def test_missed_clock_in_ignores_shifts_older_than_the_lookback_window(app, venue, monkeypatch):
    """Without a cap, the first run after enabling this would trawl the
    venue's entire history and flood admins with alerts for weeks-old
    shifts nobody clocked in for - and nobody cares about any more."""
    from scripts.check_shift_notifications import check_missed_clock_ins

    sent = []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: sent.append(a) or True)
    _enable_notification(app, venue, "missed_clock_in", venue["owner_person_id"])

    person_id, _m, _e = create_active_staff(app, venue["id"], name="Ancient Shift")
    old_date = (date.today() - timedelta(days=10)).isoformat()
    _make_scheduled_shift(app, venue["id"], person_id, old_date, "09:00", "17:00")

    with app.app_context():
        conn = db_module.get_db()
        count = check_missed_clock_ins(conn, datetime.now())

    assert count == 0
    assert sent == []


def test_missed_clock_out_not_flagged_once_clocked_out(app, venue, monkeypatch):
    from scripts.check_shift_notifications import check_missed_clock_outs

    sent = []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: sent.append(a) or True)
    _enable_notification(app, venue, "missed_clock_out", venue["owner_person_id"])

    person_id, _m, _e = create_active_staff(app, venue["id"], name="Clocked Out Fine")
    today = date.today()
    shift_end = datetime.combine(today, datetime.min.time()) + timedelta(hours=17)
    shift_id = _make_scheduled_shift(app, venue["id"], person_id, today.isoformat(), "09:00", "17:00")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at) VALUES (?, ?, ?)",
            (shift_id, datetime.now().isoformat(), datetime.now().isoformat()),
        )
        conn.commit()

    now = shift_end + timedelta(minutes=20)
    with app.app_context():
        conn = db_module.get_db()
        count = check_missed_clock_outs(conn, now)

    assert count == 0
    assert sent == []
