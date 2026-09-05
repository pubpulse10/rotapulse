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


def _set_mobile(app, person_id, mobile):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE person SET mobile = ? WHERE id = ?", (mobile, person_id))
        conn.commit()


def test_staff_reminder_sms_sent_after_grace_period(app, venue, monkeypatch):
    from scripts.check_shift_notifications import remind_staff_to_clock_in

    sms_sent = []
    monkeypatch.setattr("app.notification_settings.send_sms", lambda *a, **k: sms_sent.append(a) or True)

    person_id, _m, _e = create_active_staff(app, venue["id"], name="Needs A Nudge")
    _set_mobile(app, person_id, "07700900123")
    today = date.today()
    shift_start = datetime.combine(today, datetime.min.time()) + timedelta(hours=9)
    _make_scheduled_shift(app, venue["id"], person_id, today.isoformat(), "09:00", "17:00")

    now = shift_start + timedelta(minutes=11)  # past the 10 min grace
    with app.app_context():
        conn = db_module.get_db()
        count = remind_staff_to_clock_in(conn, now)

    assert count == 1
    assert len(sms_sent) == 1
    assert sms_sent[0][0] == "07700900123"
    assert "Needs A Nudge" in sms_sent[0][1]


def test_staff_reminder_not_sent_within_grace_period(app, venue, monkeypatch):
    from scripts.check_shift_notifications import remind_staff_to_clock_in

    sms_sent = []
    monkeypatch.setattr("app.notification_settings.send_sms", lambda *a, **k: sms_sent.append(a) or True)

    person_id, _m, _e = create_active_staff(app, venue["id"], name="On Time Ish")
    _set_mobile(app, person_id, "07700900123")
    today = date.today()
    shift_start = datetime.combine(today, datetime.min.time()) + timedelta(hours=9)
    _make_scheduled_shift(app, venue["id"], person_id, today.isoformat(), "09:00", "17:00")

    now = shift_start + timedelta(minutes=5)  # still within the 10 min grace
    with app.app_context():
        conn = db_module.get_db()
        count = remind_staff_to_clock_in(conn, now)

    assert count == 0
    assert sms_sent == []


def test_staff_reminder_not_sent_once_clocked_in(app, venue, monkeypatch):
    from scripts.check_shift_notifications import remind_staff_to_clock_in

    sms_sent = []
    monkeypatch.setattr("app.notification_settings.send_sms", lambda *a, **k: sms_sent.append(a) or True)

    person_id, _m, _e = create_active_staff(app, venue["id"], name="Already Here")
    _set_mobile(app, person_id, "07700900123")
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

    now = shift_start + timedelta(minutes=11)
    with app.app_context():
        conn = db_module.get_db()
        count = remind_staff_to_clock_in(conn, now)

    assert count == 0
    assert sms_sent == []


def test_staff_reminder_only_sent_once_across_repeated_runs(app, venue, monkeypatch):
    from scripts.check_shift_notifications import remind_staff_to_clock_in

    sms_sent = []
    monkeypatch.setattr("app.notification_settings.send_sms", lambda *a, **k: sms_sent.append(a) or True)

    person_id, _m, _e = create_active_staff(app, venue["id"], name="Checked Twice")
    _set_mobile(app, person_id, "07700900123")
    today = date.today()
    shift_start = datetime.combine(today, datetime.min.time()) + timedelta(hours=9)
    _make_scheduled_shift(app, venue["id"], person_id, today.isoformat(), "09:00", "17:00")

    now = shift_start + timedelta(minutes=11)
    with app.app_context():
        conn = db_module.get_db()
        first_count = remind_staff_to_clock_in(conn, now)
        second_count = remind_staff_to_clock_in(conn, now + timedelta(minutes=5))

    assert first_count == 1
    assert second_count == 0
    assert len(sms_sent) == 1


def test_staff_reminder_skipped_gracefully_with_no_mobile_on_file(app, venue, monkeypatch):
    from scripts.check_shift_notifications import remind_staff_to_clock_in

    sms_sent = []
    monkeypatch.setattr("app.notification_settings.send_sms", lambda *a, **k: sms_sent.append(a) or True)

    person_id, _m, _e = create_active_staff(app, venue["id"], name="No Mobile On File")
    today = date.today()
    shift_start = datetime.combine(today, datetime.min.time()) + timedelta(hours=9)
    _make_scheduled_shift(app, venue["id"], person_id, today.isoformat(), "09:00", "17:00")

    now = shift_start + timedelta(minutes=11)
    with app.app_context():
        conn = db_module.get_db()
        count = remind_staff_to_clock_in(conn, now)

    assert count == 0  # nothing delivered, but no crash
    assert sms_sent == []


def test_staff_reminder_and_admin_missed_clock_in_notice_both_fire_independently(app, venue, monkeypatch):
    """The two checks use separate shift_notification_log entries -- confirm
    neither one's dedup accidentally suppresses the other for the same
    shift."""
    from scripts.check_shift_notifications import check_missed_clock_ins, remind_staff_to_clock_in

    sms_sent, emails_sent = [], []
    monkeypatch.setattr("app.notification_settings.send_sms", lambda *a, **k: sms_sent.append(a) or True)
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: emails_sent.append(a) or True)
    _enable_notification(app, venue, "missed_clock_in", venue["owner_person_id"])

    person_id, _m, _e = create_active_staff(app, venue["id"], name="Double Checked")
    _set_mobile(app, person_id, "07700900123")
    today = date.today()
    shift_start = datetime.combine(today, datetime.min.time()) + timedelta(hours=9)
    _make_scheduled_shift(app, venue["id"], person_id, today.isoformat(), "09:00", "17:00")

    now = shift_start + timedelta(minutes=20)  # past both the 10 min and 15 min grace periods
    with app.app_context():
        conn = db_module.get_db()
        reminder_count = remind_staff_to_clock_in(conn, now)
        admin_count = check_missed_clock_ins(conn, now)

    assert reminder_count == 1
    assert admin_count == 1
    assert len(sms_sent) == 1
    assert len(emails_sent) == 1


# --------------------------------------------------------------------------- #
# Overnight shifts
#
# check_missed_clock_outs built a shift's end as (shift_date || ' ' || end_time),
# which for a 20:00-02:00 late shift put the end 24 hours early. That landed
# the shift inside the alert window while the person was still behind the bar,
# so an admin got "hasn't clocked out" mid-shift — and because
# _already_considered then marked it done, the genuine alert after they really
# finished never arrived. Same root cause as the £0 overnight costing bug (see
# tests/test_overnight_shift_cost.py); found alongside it in the 2026-09-04
# sweep.
# --------------------------------------------------------------------------- #

def _clocked_in_overnight_shift(app, venue, person_name="Late Shift"):
    """A 20:00-02:00 shift, clocked in and not yet out."""
    person_id, _m, _e = create_active_staff(app, venue["id"], name=person_name)
    today = date.today()
    shift_id = _make_scheduled_shift(
        app, venue["id"], person_id, today.isoformat(), "20:00", "02:00"
    )
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at) VALUES (?, ?)",
            (shift_id, datetime.now().isoformat()),
        )
        conn.commit()
    return today


def test_no_missed_clock_out_alert_while_the_overnight_shift_is_still_running(
    app, venue, monkeypatch
):
    """22:00 on the night of the shift: they clocked in two hours ago and have
    four to go. Nothing should fire."""
    from scripts.check_shift_notifications import check_missed_clock_outs

    sent = []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: sent.append(a) or True)
    _enable_notification(app, venue, "missed_clock_out", venue["owner_person_id"])
    today = _clocked_in_overnight_shift(app, venue)

    mid_shift = datetime.combine(today, datetime.min.time()) + timedelta(hours=22)
    with app.app_context():
        conn = db_module.get_db()
        count = check_missed_clock_outs(conn, mid_shift)

    assert count == 0, "alerted mid-shift — the end was being read as the same day"
    assert sent == []


def test_the_overnight_alert_does_fire_once_the_shift_has_really_ended(
    app, venue, monkeypatch
):
    """The other half: 02:20 the next morning, twenty minutes past a 02:00
    finish, with no clock-out. That is a genuine missed clock-out."""
    from scripts.check_shift_notifications import check_missed_clock_outs

    sent = []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: sent.append(a) or True)
    _enable_notification(app, venue, "missed_clock_out", venue["owner_person_id"])
    today = _clocked_in_overnight_shift(app, venue)

    after = datetime.combine(today, datetime.min.time()) + timedelta(days=1, hours=2, minutes=20)
    with app.app_context():
        conn = db_module.get_db()
        count = check_missed_clock_outs(conn, after)

    assert count == 1
    assert "Late Shift" in sent[0][2]


def test_an_ordinary_day_shift_is_unaffected(app, venue, monkeypatch):
    """The wrap only applies when end is earlier than start; a normal shift
    must behave exactly as before."""
    from scripts.check_shift_notifications import check_missed_clock_outs

    sent = []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: sent.append(a) or True)
    _enable_notification(app, venue, "missed_clock_out", venue["owner_person_id"])

    person_id, _m, _e = create_active_staff(app, venue["id"], name="Day Shift")
    today = date.today()
    shift_id = _make_scheduled_shift(app, venue["id"], person_id, today.isoformat(), "09:00", "17:00")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at) VALUES (?, ?)",
            (shift_id, datetime.now().isoformat()),
        )
        conn.commit()

    during = datetime.combine(today, datetime.min.time()) + timedelta(hours=13)
    after = datetime.combine(today, datetime.min.time()) + timedelta(hours=17, minutes=20)
    with app.app_context():
        conn = db_module.get_db()
        assert check_missed_clock_outs(conn, during) == 0
        assert check_missed_clock_outs(conn, after) == 1
