"""
Blocked dates and leave for everyone at once.

Step 3 of docs/leave-design.md. A customer asked to be able to block dates out
of the holiday request system because some events need full cover, and the
owner's own pub shuts the first week in January and puts every member of staff
on holiday by hand, one at a time.

The rules worth guarding, because each one is easy to get backwards:

* A block with NO roles attached applies to the whole venue. An absent row
  means everyone, not nobody.
* A block WITH roles does not catch somebody who has no role set. They are not
  in the group, and guessing would refuse leave the landlord never meant to.
* It stops STAFF requesting. The admin who set the block can still book over
  it, with a warning — there are always exceptions.
* Sick and maternity are never blocked. You cannot block somebody being ill.
* A partial overlap names the clashing dates rather than refusing the whole
  booking: "the 12th is blocked" can be acted on, "your request clashes"
  cannot.
"""

from datetime import timedelta

from app import db as db_module
from app.leave import BLOCK_NOTE_MAX, blocked_dates_for
from app.uk_time import uk_today
from tests.conftest import create_active_staff, login_as_person, login_as_pub

MON_TO_SUN = '{"mon":true,"tue":true,"wed":true,"thu":true,"fri":true,"sat":true,"sun":true}'
# The block in most tests below: Mon 3rd to Fri 7th August 2026.
BLOCK_START, BLOCK_END = "2026-08-03", "2026-08-07"


def _role(app, venue, name):
    """The venue fixture already seeds a "Bar staff" role, so this reuses an
    existing one by name rather than colliding with it."""
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("INSERT OR IGNORE INTO venue_role (venue_id, name) VALUES (?, ?)", (venue["id"], name))
        conn.commit()
        return conn.execute("SELECT id FROM venue_role WHERE venue_id = ? AND name = ?",
                            (venue["id"], name)).fetchone()["id"]


def _staff(app, venue, name="Block Tester", role_id=None):
    person_id, membership_id, _e = create_active_staff(app, venue["id"], name=name, role_id=role_id)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE rota_staff_detail SET availability = ? WHERE venue_membership_id = ?",
                     (MON_TO_SUN, membership_id))
        conn.commit()
    return person_id, membership_id


def _block(app, venue, start=BLOCK_START, end=BLOCK_END, note="Beer festival", role_ids=()):
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO leave_block (venue_id, start_date, end_date, note) VALUES (?, ?, ?, ?)",
            (venue["id"], start, end, note),
        )
        for role_id in role_ids:
            conn.execute("INSERT INTO leave_block_role (leave_block_id, venue_role_id) VALUES (?, ?)",
                         (cur.lastrowid, role_id))
        conn.commit()
        return cur.lastrowid


def _request(client, venue, start=BLOCK_START, end=BLOCK_END, leave_type="paid"):
    return client.post(f"/v/{venue['slug']}/staff/leave",
                       data={"start_date": start, "end_date": end, "leave_type": leave_type},
                       follow_redirects=True)


def _leave_count(app, person_id):
    with app.app_context():
        return db_module.get_db().execute(
            "SELECT COUNT(*) AS n FROM leave_request WHERE person_id = ?", (person_id,)
        ).fetchone()["n"]


# --------------------------------------------------------------------------- #
# Staff cannot request leave over a blocked date
# --------------------------------------------------------------------------- #

def test_a_staff_request_over_a_blocked_date_is_refused_and_says_why(app, client, venue):
    person_id, _m = _staff(app, venue)
    _block(app, venue)
    login_as_person(client, person_id)

    html = _request(client, venue).data.decode("utf-8")

    assert "Sorry" in html and "leave isn&#39;t available on" in html
    assert "Beer festival" in html          # the note, so they know what it is
    assert _leave_count(app, person_id) == 0


def test_a_partial_overlap_names_the_clashing_dates_not_the_whole_booking(app, client, venue):
    """Being told "your request clashes" is no use. Being told which days are
    the problem means the request can be re-made a day shorter."""
    person_id, _m = _staff(app, venue)
    _block(app, venue, start="2026-08-06", end="2026-08-07", note="Carnival")
    login_as_person(client, person_id)

    html = _request(client, venue, start="2026-08-03", end="2026-08-07").data.decode("utf-8")

    assert "6 August 2026" in html and "7 August 2026" in html
    assert "3 August 2026" not in html  # the days that are fine aren't named


def test_three_clashing_dates_read_as_a_list_not_a_chain_of_ands(app, client, venue):
    person_id, _m = _staff(app, venue)
    _block(app, venue, start="2026-08-05", end="2026-08-07", note="Carnival")
    login_as_person(client, person_id)

    html = _request(client, venue, start="2026-08-05", end="2026-08-07").data.decode("utf-8")

    assert "5 August 2026, 6 August 2026 and 7 August 2026" in html


def test_a_long_block_is_summarised_rather_than_listed_one_date_at_a_time(app, client, venue):
    person_id, _m = _staff(app, venue)
    _block(app, venue, start="2026-08-01", end="2026-08-31", note="Refurbishment")
    login_as_person(client, person_id)

    html = _request(client, venue, start="2026-08-03", end="2026-08-14").data.decode("utf-8")

    assert "12 dates between" in html
    assert "Refurbishment" in html


def test_leave_either_side_of_a_block_is_still_accepted(app, client, venue):
    person_id, _m = _staff(app, venue)
    _block(app, venue)
    login_as_person(client, person_id)

    _request(client, venue, start="2026-08-10", end="2026-08-14")

    assert _leave_count(app, person_id) == 1


def test_sick_and_maternity_are_never_blocked(app, client, venue):
    """You cannot block somebody being ill. Both are admin-recorded, so the
    check is on the rule itself rather than through the staff form."""
    person_id, _m = _staff(app, venue)
    _block(app, venue)

    with app.app_context():
        db = db_module.get_db()
        assert blocked_dates_for(db, venue["id"], person_id, BLOCK_START, BLOCK_END, "sick") == []
        assert blocked_dates_for(db, venue["id"], person_id, BLOCK_START, BLOCK_END, "maternity") == []
        assert blocked_dates_for(db, venue["id"], person_id, BLOCK_START, BLOCK_END, "paid") != []


# --------------------------------------------------------------------------- #
# Who a block applies to
# --------------------------------------------------------------------------- #

def test_a_block_with_no_roles_applies_to_everyone(app, client, venue):
    """An absent row means everyone, not nobody — so a block whose roles were
    later deleted keeps blocking instead of silently going quiet."""
    kitchen = _role(app, venue, "Kitchen staff")
    person_id, _m = _staff(app, venue, role_id=kitchen)
    _block(app, venue, role_ids=())
    login_as_person(client, person_id)

    _request(client, venue)

    assert _leave_count(app, person_id) == 0


def test_a_block_scoped_to_a_group_catches_that_group(app, client, venue):
    kitchen = _role(app, venue, "Kitchen staff")
    person_id, _m = _staff(app, venue, name="Chef", role_id=kitchen)
    _block(app, venue, role_ids=(kitchen,))
    login_as_person(client, person_id)

    _request(client, venue)

    assert _leave_count(app, person_id) == 0


def test_a_block_scoped_to_a_group_leaves_other_groups_alone(app, client, venue):
    kitchen = _role(app, venue, "Kitchen staff")
    bar = _role(app, venue, "Bar staff")
    person_id, _m = _staff(app, venue, name="Bar Person", role_id=bar)
    _block(app, venue, role_ids=(kitchen,))
    login_as_person(client, person_id)

    _request(client, venue)

    assert _leave_count(app, person_id) == 1


def test_somebody_with_no_group_set_is_not_caught_by_a_group_block(app, client, venue):
    """They are not in the group. Guessing that they might be would refuse
    leave the landlord never meant to refuse — and the admin form says so."""
    kitchen = _role(app, venue, "Kitchen staff")
    person_id, _m = _staff(app, venue, name="Ungrouped", role_id=None)
    _block(app, venue, role_ids=(kitchen,))
    login_as_person(client, person_id)

    _request(client, venue)

    assert _leave_count(app, person_id) == 1


# --------------------------------------------------------------------------- #
# What people see
# --------------------------------------------------------------------------- #

def test_blocked_dates_are_listed_on_the_request_form_before_they_pick(app, client, venue):
    person_id, _m = _staff(app, venue)
    ahead = uk_today() + timedelta(days=30)
    _block(app, venue, start=ahead.isoformat(), end=ahead.isoformat(), note="Village fete")
    login_as_person(client, person_id)

    html = client.get(f"/v/{venue['slug']}/staff/leave").data.decode("utf-8")

    assert "Leave isn't available on these dates" in html
    assert "Village fete" in html


def test_a_block_that_has_finished_is_not_still_shown_to_staff(app, client, venue):
    person_id, _m = _staff(app, venue)
    gone = uk_today() - timedelta(days=30)
    _block(app, venue, start=gone.isoformat(), end=gone.isoformat(), note="Last years fete")
    login_as_person(client, person_id)

    html = client.get(f"/v/{venue['slug']}/staff/leave").data.decode("utf-8")

    assert "Last years fete" not in html


def test_a_group_block_is_not_shown_to_staff_it_does_not_apply_to(app, client, venue):
    kitchen = _role(app, venue, "Kitchen staff")
    person_id, _m = _staff(app, venue, name="Bar Only")
    ahead = uk_today() + timedelta(days=30)
    _block(app, venue, start=ahead.isoformat(), end=ahead.isoformat(), note="Kitchen deep clean",
           role_ids=(kitchen,))
    login_as_person(client, person_id)

    html = client.get(f"/v/{venue['slug']}/staff/leave").data.decode("utf-8")

    assert "Kitchen deep clean" not in html


def test_the_rota_grid_shows_the_block_with_its_note(app, client, venue):
    """So the day explains itself where the week is actually looked at."""
    _staff(app, venue)
    _block(app, venue, note="Beer festival")
    login_as_pub(client, venue["pub_id"])

    html = client.get(f"/v/{venue['slug']}/rota/?week={BLOCK_START}").data.decode("utf-8")

    assert "blocked-badge" in html
    assert "Beer festival" in html


# --------------------------------------------------------------------------- #
# The admin is not handcuffed by their own block
# --------------------------------------------------------------------------- #

def test_an_admin_can_still_record_leave_over_a_blocked_date_but_is_warned(app, client, venue):
    person_id, _m = _staff(app, venue)
    _block(app, venue)
    login_as_pub(client, venue["pub_id"])

    html = client.post(f"/v/{venue['slug']}/rota/leave/create",
                       data={"person_id": person_id, "start_date": BLOCK_START,
                             "end_date": BLOCK_END, "leave_type": "paid"},
                       follow_redirects=True).data.decode("utf-8")

    assert _leave_count(app, person_id) == 1   # it went through
    assert "Leave added, but note these dates are blocked" in html
    assert "Beer festival" in html


# --------------------------------------------------------------------------- #
# Managing the blocks
# --------------------------------------------------------------------------- #

def _create_block(client, venue, **data):
    payload = {"start_date": BLOCK_START, "end_date": BLOCK_END, "note": "Beer festival"}
    payload.update(data)
    return client.post(f"/v/{venue['slug']}/rota/leave/blocked/create", data=payload,
                       follow_redirects=True)


def _blocks(app, venue):
    with app.app_context():
        return db_module.get_db().execute(
            "SELECT * FROM leave_block WHERE venue_id = ? ORDER BY id", (venue["id"],)
        ).fetchall()


def test_an_admin_can_block_dates_out(app, client, venue):
    login_as_pub(client, venue["pub_id"])

    _create_block(client, venue)

    rows = _blocks(app, venue)
    assert len(rows) == 1
    assert rows[0]["start_date"] == BLOCK_START and rows[0]["note"] == "Beer festival"


def test_the_note_is_capped_at_twenty_characters(app, client, venue):
    """The owner set the figure: it sits in a rota day-header, not a
    paragraph."""
    login_as_pub(client, venue["pub_id"])

    _create_block(client, venue, note="A" * 60)

    assert len(_blocks(app, venue)[0]["note"]) == BLOCK_NOTE_MAX == 20


def test_a_backwards_date_range_is_refused(app, client, venue):
    login_as_pub(client, venue["pub_id"])

    _create_block(client, venue, start_date=BLOCK_END, end_date=BLOCK_START)

    assert _blocks(app, venue) == []


def test_a_role_from_another_venue_cannot_be_attached(app, client, venue):
    """Role ids arrive from a form. Blocking is fail-closed: an id we don't
    recognise is dropped, which leaves the block applying to everyone here
    rather than reaching into another pub's roles."""
    with app.app_context():
        conn = db_module.get_db()
        other = conn.execute(
            "INSERT INTO venue (pub_id, name, slug) VALUES ('pub-other', 'Other', 'other')").lastrowid
        foreign_role = conn.execute(
            "INSERT INTO venue_role (venue_id, name) VALUES (?, 'Their staff')", (other,)).lastrowid
        conn.commit()
    login_as_pub(client, venue["pub_id"])

    _create_block(client, venue, role_ids=str(foreign_role))

    block_id = _blocks(app, venue)[0]["id"]
    with app.app_context():
        attached = db_module.get_db().execute(
            "SELECT COUNT(*) AS n FROM leave_block_role WHERE leave_block_id = ?", (block_id,)
        ).fetchone()["n"]
    assert attached == 0


def test_unblocking_removes_the_block_and_its_groups(app, client, venue):
    kitchen = _role(app, venue, "Kitchen staff")
    block_id = _block(app, venue, role_ids=(kitchen,))
    login_as_pub(client, venue["pub_id"])

    client.post(f"/v/{venue['slug']}/rota/leave/blocked/{block_id}/delete", follow_redirects=True)

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT COUNT(*) AS n FROM leave_block").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM leave_block_role").fetchone()["n"] == 0


def test_a_role_used_by_a_blocked_date_cannot_be_deleted(app, client, venue):
    """The block holds a foreign key to the role, so deleting it would crash.
    Refusing is also the only safe answer: dropping the link would leave the
    block with no roles, and a block with no roles applies to EVERYONE — so
    "kitchen staff can't book" would quietly become "nobody can"."""
    kitchen = _role(app, venue, "Kitchen staff")
    _block(app, venue, role_ids=(kitchen,))
    login_as_pub(client, venue["pub_id"])

    html = client.post(f"/v/{venue['slug']}/admin/roles/{kitchen}/delete",
                       follow_redirects=True).data.decode("utf-8")

    assert "blocked date" in html
    with app.app_context():
        assert db_module.get_db().execute(
            "SELECT COUNT(*) AS n FROM venue_role WHERE id = ?", (kitchen,)).fetchone()["n"] == 1


def test_staff_cannot_block_dates_out(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_person(client, person_id)

    _create_block(client, venue)

    assert _blocks(app, venue) == []


# --------------------------------------------------------------------------- #
# Leave for everyone at once
# --------------------------------------------------------------------------- #

def _bulk(client, venue, **data):
    payload = {"start_date": "2027-01-04", "end_date": "2027-01-08", "leave_type": "paid"}
    payload.update(data)
    return client.post(f"/v/{venue['slug']}/rota/leave/bulk", data=payload, follow_redirects=True)


def test_leave_for_everyone_adds_it_to_every_member_of_staff(app, client, venue):
    first, _m = _staff(app, venue, name="Aaa Person")
    second, _m2 = _staff(app, venue, name="Bbb Person")
    login_as_pub(client, venue["pub_id"])

    _bulk(client, venue)

    assert _leave_count(app, first) == 1
    assert _leave_count(app, second) == 1


def test_bulk_leave_is_approved_and_counted_straight_away(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])

    _bulk(client, venue)

    with app.app_context():
        row = db_module.get_db().execute(
            "SELECT * FROM leave_request WHERE person_id = ?", (person_id,)).fetchone()
    assert row["status"] == "approved"
    # Frozen at approval, so editing an availability later can't rewrite it.
    assert row["days_counted"] == 5


def test_bulk_leave_skips_anybody_already_booked_and_names_them(app, client, venue):
    """Re-running it must not double-book somebody, and quietly overwriting an
    existing booking would be worse than skipping it."""
    booked, _m = _staff(app, venue, name="Already Booked")
    free, _m2 = _staff(app, venue, name="Wide Open")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("""INSERT INTO leave_request (person_id, venue_id, start_date, end_date, status)
                        VALUES (?, ?, '2027-01-06', '2027-01-06', 'approved')""", (booked, venue["id"]))
        conn.commit()
    login_as_pub(client, venue["pub_id"])

    html = _bulk(client, venue).data.decode("utf-8")

    assert _leave_count(app, booked) == 1   # untouched
    assert _leave_count(app, free) == 1
    assert "Already Booked" in html
    assert "Leave added for 1 staff member." in html


def test_bulk_leave_skips_a_pending_request_too(app, client, venue):
    """A request still waiting on a decision is a booking in progress —
    approving a second overlapping one behind their back is not helpful."""
    person_id, _m = _staff(app, venue)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("""INSERT INTO leave_request (person_id, venue_id, start_date, end_date, status)
                        VALUES (?, ?, '2027-01-05', '2027-01-05', 'pending')""", (person_id, venue["id"]))
        conn.commit()
    login_as_pub(client, venue["pub_id"])

    _bulk(client, venue)

    assert _leave_count(app, person_id) == 1


def test_bulk_leave_can_be_a_type_other_than_holiday(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])

    _bulk(client, venue, leave_type="unpaid")

    with app.app_context():
        row = db_module.get_db().execute(
            "SELECT leave_type FROM leave_request WHERE person_id = ?", (person_id,)).fetchone()
    assert row["leave_type"] == "unpaid"


def test_bulk_leave_refuses_a_backwards_range(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_pub(client, venue["pub_id"])

    _bulk(client, venue, start_date="2027-01-08", end_date="2027-01-04")

    assert _leave_count(app, person_id) == 0


def test_staff_cannot_add_leave_for_everyone(app, client, venue):
    person_id, _m = _staff(app, venue)
    login_as_person(client, person_id)

    _bulk(client, venue)

    assert _leave_count(app, person_id) == 0
