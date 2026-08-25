from datetime import date, timedelta

from app import db as db_module
from tests.conftest import create_active_staff, login_as_pub


def test_week_grid_shows_shift_cell(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _membership_id, _email = create_active_staff(app, venue["id"], name="Alex")

    today = date.today().isoformat()
    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/create",
        data={"person_id": person_id, "shift_date": today, "start_time": "09:00", "end_time": "17:00"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"09:00" in resp.data


def test_avatar_is_pinned_to_the_staff_name_not_each_shift_chip(app, client, venue):
    """Real request, 2026-08-18: the photo used to render inside every
    individual shift chip. Now it should appear once, next to the staff
    member's name in the leftmost column, regardless of how many shifts
    they have that week."""
    login_as_pub(client, venue["pub_id"])
    person_id, _membership_id, _email = create_active_staff(app, venue["id"], name="Alex")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE person SET avatar_url = 'alex.png' WHERE id = ?", (person_id,))
        conn.commit()

    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    for d in (today, tomorrow):
        client.post(
            f"/v/{venue['slug']}/rota/shift/create",
            data={"person_id": person_id, "shift_date": d, "start_time": "09:00", "end_time": "17:00"},
        )

    resp = client.get(f"/v/{venue['slug']}/rota/")
    body = resp.data.decode()
    assert body.count('class="avatar"') == 1  # one photo total, not one per shift
    assert 'rota-name-cell' in body
    name_cell = body.split('class="rota-name-cell"')[1].split("</td>")[0]
    assert "avatar" in name_cell
    assert "media/avatar/alex.png" in name_cell


def test_event_tag_can_be_added_and_removed_from_the_grid(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    today = date.today().isoformat()

    resp = client.post(
        f"/v/{venue['slug']}/rota/event-tag/create",
        data={"tag_date": today, "label": "Quiz night"},
        follow_redirects=True,
    )
    assert b"Quiz night" in resp.data
    assert b"event-badge-remove" in resp.data  # the new remove control is present

    with app.app_context():
        conn = db_module.get_db()
        tag = conn.execute("SELECT id FROM event_tag WHERE venue_id = ? AND tag_date = ?", (venue["id"], today)).fetchone()

    resp = client.post(
        f"/v/{venue['slug']}/rota/event-tag/{tag['id']}/delete",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Quiz night" not in resp.data

    with app.app_context():
        conn = db_module.get_db()
        remaining = conn.execute("SELECT 1 FROM event_tag WHERE id = ?", (tag["id"],)).fetchone()
        assert remaining is None


def test_day_header_shows_total_cost_for_that_day(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _membership_id, _email = create_active_staff(app, venue["id"], name="CostCheck")

    today = date.today().isoformat()
    client.post(
        f"/v/{venue['slug']}/rota/shift/create",
        data={"person_id": person_id, "shift_date": today, "start_time": "09:00", "end_time": "17:00"},
    )
    resp = client.get(f"/v/{venue['slug']}/rota/")
    # 8 hours * £12.50/hr (create_active_staff's fixed rate) = £100.00
    assert b"day-cost" in resp.data
    assert "£100.00".encode() in resp.data


def test_staff_header_shows_total_cost_for_the_week(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _membership_id, _email = create_active_staff(app, venue["id"], name="WeekCostCheck")

    monday = date.today() - timedelta(days=date.today().weekday())
    for offset in (0, 1):  # two 8-hour shifts this week = £200.00 total
        shift_date = (monday + timedelta(days=offset)).isoformat()
        client.post(
            f"/v/{venue['slug']}/rota/shift/create",
            data={"person_id": person_id, "shift_date": shift_date, "start_time": "09:00", "end_time": "17:00"},
        )

    resp = client.get(f"/v/{venue['slug']}/rota/")
    text = resp.data.decode()
    thead_start = text.index("<thead>")
    staff_header = text[thead_start : text.index("Staff", thead_start)]
    assert "£200.00" in staff_header


def test_empty_cell_is_dashed_plus_reachable(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    create_active_staff(app, venue["id"], name="Sam")
    resp = client.get(f"/v/{venue['slug']}/rota/")
    assert resp.status_code == 200
    assert b"cell-empty" in resp.data


def test_day_off_override_shows_bed_icon(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _membership_id, _email = create_active_staff(app, venue["id"], name="Jo")
    today = date.today().isoformat()

    client.post(
        f"/v/{venue['slug']}/rota/day-off-override/create",
        data={"person_id": person_id, "override_date": today, "note": "Dentist"},
        follow_redirects=True,
    )
    resp = client.get(f"/v/{venue['slug']}/rota/")
    assert b"cell-dayoff" in resp.data


def test_non_admin_cannot_access_rota(client, venue):
    resp = client.get(f"/v/{venue['slug']}/rota/", follow_redirects=False)
    assert resp.status_code == 302


def _create_shift(app, venue_id, person_id, shift_date):
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')",
            (venue_id, person_id, shift_date),
        )
        conn.commit()
        return cur.lastrowid


def test_move_shift_to_different_person_and_date(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    from_person, _m1, _e1 = create_active_staff(app, venue["id"], name="From")
    to_person, _m2, _e2 = create_active_staff(app, venue["id"], name="To")
    shift_id = _create_shift(app, venue["id"], from_person, "2026-08-03")

    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/move",
        json={"person_id": to_person, "shift_date": "2026-08-04"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT * FROM shift WHERE id = ?", (shift_id,)).fetchone()
        assert row["person_id"] == to_person
        assert row["shift_date"] == "2026-08-04"


def test_move_shift_blocked_once_it_has_attendance_recorded(app, client, venue):
    """Real bug, confirmed live: attendance is keyed by shift_id, not by
    date, so moving a shift that had already been clocked in/out for used
    to silently carry that stale clock-in/out data to the new person/date
    -- a shift moved to today kept showing "clocked in" from several days
    earlier, which also made the missed-clock-in/staff-reminder checks
    wrongly think the (new) occurrence was already covered."""
    login_as_pub(client, venue["pub_id"])
    from_person, _m1, _e1 = create_active_staff(app, venue["id"], name="AlreadyWorked")
    to_person, _m2, _e2 = create_active_staff(app, venue["id"], name="MoveTarget")
    shift_id = _create_shift(app, venue["id"], from_person, "2026-08-03")

    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at) VALUES (?, '2026-08-03T09:00:00', '2026-08-03T17:00:00')",
            (shift_id,),
        )
        conn.commit()

    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/move",
        json={"person_id": to_person, "shift_date": "2026-08-10"},
    )
    assert resp.status_code == 409
    assert "already has clock-in" in resp.get_json()["error"].lower()

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT person_id, shift_date FROM shift WHERE id = ?", (shift_id,)).fetchone()
        assert row["person_id"] == from_person  # unchanged
        assert row["shift_date"] == "2026-08-03"  # unchanged


def test_move_shift_blocked_by_approved_leave(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    from_person, _m1, _e1 = create_active_staff(app, venue["id"], name="Mover")
    target_person, _m2, _e2 = create_active_staff(app, venue["id"], name="OnLeave")
    shift_id = _create_shift(app, venue["id"], from_person, "2026-08-03")

    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO leave_request (person_id, venue_id, start_date, end_date, status) VALUES (?, ?, '2026-08-04', '2026-08-04', 'approved')",
            (target_person, venue["id"]),
        )
        conn.commit()

    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/move",
        json={"person_id": target_person, "shift_date": "2026-08-04"},
    )
    assert resp.status_code == 409
    assert "leave" in resp.get_json()["error"].lower()

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT person_id FROM shift WHERE id = ?", (shift_id,)).fetchone()
        assert row["person_id"] == from_person  # unchanged


def test_move_shift_blocked_when_target_already_has_a_shift(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    from_person, _m1, _e1 = create_active_staff(app, venue["id"], name="Dragged")
    target_person, _m2, _e2 = create_active_staff(app, venue["id"], name="AlreadyBusy")
    shift_id = _create_shift(app, venue["id"], from_person, "2026-08-03")
    _create_shift(app, venue["id"], target_person, "2026-08-04")  # target already booked that day

    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/move",
        json={"person_id": target_person, "shift_date": "2026-08-04"},
    )
    assert resp.status_code == 409
    assert "already has a shift" in resp.get_json()["error"].lower()

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT person_id FROM shift WHERE id = ?", (shift_id,)).fetchone()
        assert row["person_id"] == from_person  # unchanged
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM shift WHERE person_id = ? AND shift_date = '2026-08-04'", (target_person,)
        ).fetchone()["n"]
        assert count == 1  # still just the original shift, nothing combined


def test_moving_a_shift_back_onto_its_own_slot_is_a_no_op_not_a_conflict(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="SelfMove")
    shift_id = _create_shift(app, venue["id"], person_id, "2026-08-03")

    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/move",
        json={"person_id": person_id, "shift_date": "2026-08-03"},
    )
    assert resp.status_code == 200


def test_move_shift_rejects_non_staff_target(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    from_person, _m1, _e1 = create_active_staff(app, venue["id"], name="Mover2")
    shift_id = _create_shift(app, venue["id"], from_person, "2026-08-03")

    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/move",
        json={"person_id": 999999, "shift_date": "2026-08-04"},
    )
    assert resp.status_code == 400


def test_dragging_a_scheduled_shift_onto_the_open_row_unassigns_it(app, client, venue):
    """The Open row is now a real drag target (2026-08-19): dropping a
    person's shift there is the drag equivalent of the existing "Mark as
    open" button — no person_id in the JSON body is what signals this
    (see week.html's Open-row cells, which have no data-person-id)."""
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Unavailable")
    role_id = venue["role_id"]
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, venue_role_id, shift_date, start_time, end_time, status) "
            "VALUES (?, ?, ?, '2026-08-03', '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id, role_id),
        ).lastrowid
        conn.commit()

    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/move",
        json={"shift_date": "2026-08-04"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT * FROM shift WHERE id = ?", (shift_id,)).fetchone()
        assert row["status"] == "open"
        assert row["person_id"] is None
        assert row["shift_date"] == "2026-08-04"
        # Role kept — this is what notify_open_shift's role-targeted alert relies on.
        assert row["venue_role_id"] == role_id


def test_dragging_an_open_shift_onto_a_person_claims_it(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Claimant")
    with app.app_context():
        conn = db_module.get_db()
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) "
            "VALUES (?, NULL, '2026-08-03', '09:00', '17:00', 'open')",
            (venue["id"],),
        ).lastrowid
        conn.commit()

    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/move",
        json={"person_id": person_id, "shift_date": "2026-08-03"},
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT * FROM shift WHERE id = ?", (shift_id,)).fetchone()
        assert row["status"] == "scheduled"
        assert row["person_id"] == person_id


def test_dragging_an_open_shift_onto_a_person_still_blocked_by_conflicts(app, client, venue):
    """Claiming via drag goes through the exact same guards as any other
    move — approved leave here, but leave/already-has-a-shift/not-billable
    all apply identically regardless of which direction the shift moves."""
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="OnLeaveClaimant")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO leave_request (person_id, venue_id, start_date, end_date, status) VALUES (?, ?, '2026-08-03', '2026-08-03', 'approved')",
            (person_id, venue["id"]),
        )
        shift_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) "
            "VALUES (?, NULL, '2026-08-03', '09:00', '17:00', 'open')",
            (venue["id"],),
        ).lastrowid
        conn.commit()

    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/{shift_id}/move",
        json={"person_id": person_id, "shift_date": "2026-08-03"},
    )
    assert resp.status_code == 409
    assert "leave" in resp.get_json()["error"].lower()

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT status, person_id FROM shift WHERE id = ?", (shift_id,)).fetchone()
        assert row["status"] == "open"  # unchanged
        assert row["person_id"] is None


def test_open_shift_day_lists_shifts_and_offers_add_form(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO shift (venue_id, person_id, venue_role_id, shift_date, start_time, end_time, status) "
            "VALUES (?, NULL, ?, '2026-08-03', '09:00', '17:00', 'open')",
            (venue["id"], venue["role_id"]),
        )
        conn.commit()

    resp = client.get(f"/v/{venue['slug']}/rota/open-shift/day/2026-08-03")
    assert resp.status_code == 200
    assert b"09:00-17:00" in resp.data
    assert b"Bar staff" in resp.data
    assert b"Add an open shift" in resp.data


def test_open_shift_day_works_for_a_date_with_no_open_shifts_yet(app, client, venue):
    """The new empty-cell tap target on the grid — the whole point is
    reaching the add form even when nothing exists there yet."""
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/rota/open-shift/day/2026-08-03")
    assert resp.status_code == 200
    assert b"Add an open shift" in resp.data


def test_open_shift_day_requires_admin_permission(client, venue):
    resp = client.get(f"/v/{venue['slug']}/rota/open-shift/day/2026-08-03")
    assert resp.status_code == 302


def test_move_shift_requires_admin_permission(client, venue):
    resp = client.post(
        f"/v/{venue['slug']}/rota/shift/1/move",
        json={"person_id": 1, "shift_date": "2026-08-04"},
        follow_redirects=False,
    )
    assert resp.status_code == 302


def test_copy_week_to_a_week_further_along(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Copied")
    # Source week: Mon 2026-08-03. A shift on the Tuesday (offset +1 day).
    _create_shift(app, venue["id"], person_id, "2026-08-04")

    resp = client.post(
        f"/v/{venue['slug']}/rota/copy-week",
        data={"source_week": "2026-08-03", "target_week": "2026-08-17"},  # two weeks later, not "next week"
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Copied 1 shift" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        rows = conn.execute(
            "SELECT * FROM shift WHERE person_id = ? AND shift_date = '2026-08-18'", (person_id,)  # same Tuesday offset
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["start_time"] == "09:00"
        assert rows[0]["end_time"] == "17:00"
        # Original shift must still exist, untouched.
        original = conn.execute("SELECT * FROM shift WHERE person_id = ? AND shift_date = '2026-08-04'", (person_id,)).fetchone()
        assert original is not None


def test_copy_week_skips_conflicting_target_shift(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="Conflicted")
    _create_shift(app, venue["id"], person_id, "2026-08-03")
    _create_shift(app, venue["id"], person_id, "2026-08-17")  # target already has a shift that same day

    resp = client.post(
        f"/v/{venue['slug']}/rota/copy-week",
        data={"source_week": "2026-08-03", "target_week": "2026-08-17"},
        follow_redirects=True,
    )
    assert b"Skipped 1" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM shift WHERE person_id = ? AND shift_date = '2026-08-17'", (person_id,)
        ).fetchone()["n"]
        assert count == 1  # unchanged — not duplicated/combined


def test_copy_week_skips_person_on_approved_leave(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="LeaveTarget")
    _create_shift(app, venue["id"], person_id, "2026-08-03")

    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO leave_request (person_id, venue_id, start_date, end_date, status) VALUES (?, ?, '2026-08-17', '2026-08-17', 'approved')",
            (person_id, venue["id"]),
        )
        conn.commit()

    resp = client.post(
        f"/v/{venue['slug']}/rota/copy-week",
        data={"source_week": "2026-08-03", "target_week": "2026-08-17"},
        follow_redirects=True,
    )
    assert b"Skipped 1" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT 1 FROM shift WHERE person_id = ? AND shift_date = '2026-08-17'", (person_id,)).fetchone()
        assert row is None


def test_copy_week_preserves_open_shifts(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO shift (venue_id, shift_date, start_time, end_time, status) VALUES (?, '2026-08-05', '18:00', '23:00', 'open')",
            (venue["id"],),
        )
        conn.commit()

    client.post(
        f"/v/{venue['slug']}/rota/copy-week",
        data={"source_week": "2026-08-03", "target_week": "2026-08-17"},
    )

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT * FROM shift WHERE shift_date = '2026-08-19' AND status = 'open'"  # Wed offset preserved
        ).fetchone()
        assert row is not None
        assert row["person_id"] is None


def test_copy_week_into_same_week_is_rejected(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/rota/copy-week",
        data={"source_week": "2026-08-03", "target_week": "2026-08-03"},
        follow_redirects=True,
    )
    assert b"different week" in resp.data.lower()


def test_copy_week_requires_admin_permission(client, venue):
    resp = client.post(
        f"/v/{venue['slug']}/rota/copy-week",
        data={"source_week": "2026-08-03", "target_week": "2026-08-17"},
        follow_redirects=False,
    )
    assert resp.status_code == 302


def test_clear_week_deletes_shifts_in_that_week_only(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="ToClear")
    _create_shift(app, venue["id"], person_id, "2026-08-03")  # Monday of the week being cleared
    _create_shift(app, venue["id"], person_id, "2026-08-09")  # Sunday, same week
    _create_shift(app, venue["id"], person_id, "2026-08-10")  # Monday of the NEXT week — must survive

    resp = client.post(
        f"/v/{venue['slug']}/rota/clear-week",
        data={"week": "2026-08-03"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Cleared 2 shift" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        remaining = conn.execute(
            "SELECT shift_date FROM shift WHERE venue_id = ? ORDER BY shift_date", (venue["id"],)
        ).fetchall()
        assert [r["shift_date"] for r in remaining] == ["2026-08-10"]


def test_clear_week_also_clears_open_shifts_and_attendance(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="ClearWithAttendance")

    with app.app_context():
        conn = db_module.get_db()
        scheduled_id = conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, '2026-08-04', '09:00', '17:00', 'scheduled')",
            (venue["id"], person_id),
        ).lastrowid
        conn.execute(
            "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at) VALUES (?, '2026-08-04T09:00:00', '2026-08-04T17:00:00')",
            (scheduled_id,),
        )
        conn.execute(
            "INSERT INTO shift (venue_id, shift_date, start_time, end_time, status) VALUES (?, '2026-08-05', '18:00', '23:00', 'open')",
            (venue["id"],),
        )
        conn.commit()

    client.post(f"/v/{venue['slug']}/rota/clear-week", data={"week": "2026-08-03"})

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT 1 FROM shift WHERE venue_id = ?", (venue["id"],)).fetchone() is None
        assert conn.execute("SELECT 1 FROM attendance WHERE shift_id = ?", (scheduled_id,)).fetchone() is None


def test_clear_week_requires_admin_permission(client, venue):
    resp = client.post(
        f"/v/{venue['slug']}/rota/clear-week",
        data={"week": "2026-08-03"},
        follow_redirects=False,
    )
    assert resp.status_code == 302


def test_week_starts_unnotified(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/rota/?week=2026-08-03")
    assert b"Unnotified" in resp.data
    assert b"Notify staff of this week" in resp.data


def test_week_grid_offers_a_clear_week_form_directly_on_the_page(app, client, venue):
    """Same fix as notify/copy above, requested alongside them — "Clear
    this week" also only ever lived in the buried nav dropdown."""
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/rota/?week=2026-08-03")
    assert resp.status_code == 200
    assert b"Clear this week" in resp.data
    assert f'action="/v/{venue["slug"]}/rota/clear-week"'.encode() in resp.data
    assert b'name="week" value="2026-08-03"' in resp.data


def test_week_grid_offers_a_copy_week_form_directly_on_the_page(app, client, venue):
    """Real report, 2026-08-19: an admin who'd used "copy week" before
    couldn't find it any more. It hadn't actually broken — it's always
    lived inside the "Rota" nav dropdown (base.html, two menus deep on
    mobile: hamburger -> Rota -> scroll to "Paste this week into") - easy
    to lose track of. Added a second, directly-visible copy of the same
    form right on the week page itself, same as the notify button above."""
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/rota/?week=2026-08-03")
    assert resp.status_code == 200
    assert b"Copy this week" in resp.data
    assert f'action="/v/{venue["slug"]}/rota/copy-week"'.encode() in resp.data
    assert b'id="page_target_week"' in resp.data
    assert b'name="source_week" value="2026-08-03"' in resp.data


def test_week_grid_offers_a_notify_button_when_unnotified(app, client, venue):
    """Real report, 2026-08-19: the backend route and "Unnotified"/"Notified"
    badge both existed from the app's very first commit, but no version of
    this template ever actually had a button wired to notify_week — so the
    feature was never reachable at all despite looking finished. The other
    notify_week tests below only ever assert the button's text is ABSENT
    (after sending), which is trivially true whether or not it was ever
    present in the first place — this is the one asserting it's actually
    there, and posts to the exact URL it renders."""
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/rota/?week=2026-08-03")
    assert resp.status_code == 200
    assert b"Notify staff of this week" in resp.data
    assert f'action="/v/{venue["slug"]}/rota/notify-week"'.encode() in resp.data
    assert b'name="week" value="2026-08-03"' in resp.data


def test_notify_week_sends_and_marks_notified(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="ToNotify")
    _create_shift(app, venue["id"], person_id, "2026-08-03")
    _create_shift(app, venue["id"], person_id, "2026-08-05")

    resp = client.post(
        f"/v/{venue['slug']}/rota/notify-week",
        data={"week": "2026-08-03"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Notified 1 staff member" in resp.data
    assert b"Notified" in resp.data
    assert b"Notify staff of this week" not in resp.data  # button gone now it's sent

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT * FROM week_notification WHERE venue_id = ? AND week_start_date = '2026-08-03'", (venue["id"],)
        ).fetchone()
        assert row is not None
        assert row["recipient_count"] == 1


def test_notify_week_cannot_be_sent_twice(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"], name="OnceOnly")
    _create_shift(app, venue["id"], person_id, "2026-08-03")

    client.post(f"/v/{venue['slug']}/rota/notify-week", data={"week": "2026-08-03"})
    resp = client.post(
        f"/v/{venue['slug']}/rota/notify-week",
        data={"week": "2026-08-03"},
        follow_redirects=True,
    )
    assert b"already been notified" in resp.data.lower()

    with app.app_context():
        conn = db_module.get_db()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM week_notification WHERE venue_id = ? AND week_start_date = '2026-08-03'",
            (venue["id"],),
        ).fetchone()["n"]
        assert count == 1  # not duplicated


def test_notify_week_with_no_shifts_is_not_marked_notified(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/rota/notify-week",
        data={"week": "2026-08-03"},
        follow_redirects=True,
    )
    assert b"No scheduled shifts" in resp.data

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT 1 FROM week_notification WHERE venue_id = ? AND week_start_date = '2026-08-03'", (venue["id"],)
        ).fetchone()
        assert row is None  # still unnotified — nothing was actually sent


def test_notify_week_requires_admin_permission(client, venue):
    resp = client.post(
        f"/v/{venue['slug']}/rota/notify-week",
        data={"week": "2026-08-03"},
        follow_redirects=False,
    )
    assert resp.status_code == 302


def test_copy_week_dropdown_flags_weeks_that_already_have_shifts(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    person_id, _m, _e = create_active_staff(app, venue["id"])
    _create_shift(app, venue["id"], person_id, "2026-08-17")  # a Monday, within the dropdown's range

    resp = client.get(f"/v/{venue['slug']}/rota/?week=2026-08-03")
    text = resp.data.decode()

    busy_option_idx = text.index('value="2026-08-17"')
    busy_option = text[busy_option_idx : busy_option_idx + 200]
    assert 'data-has-shifts="true"' in busy_option
    assert "(has shifts)" in busy_option

    empty_option_idx = text.index('value="2026-08-24"')  # a different Monday, no shifts
    empty_option = text[empty_option_idx : empty_option_idx + 200]
    assert 'data-has-shifts="false"' in empty_option
    assert "(has shifts)" not in empty_option
