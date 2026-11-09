"""
Pay is for the Owner and Managers only — proved route by route.

Brief, 9 November 2026: at The Cock the owner login was used on the shared bar
tablet, so anyone at the bar could see everyone's pay. The fix in the product
is that every pub gives a shared device its own Staff login — which is only
safe if a Staff login really gets no pay figures from ANY endpoint, not just
from the screens its menu happens to link to.

So rather than list the pay screens and hope the list stays complete, these
tests walk the app's whole URL map as a Staff session (both ways a staff
member can be signed in: RotaPulse's own login, and a Hub login) and check
every response body for the pay figures planted in the fixture. A route added
next year is swept automatically.

Role mapping (CLAUDE.md, 2026-11-09): Hub Owner = app_admin + rota_admin,
Hub Manager = rota_admin, Hub Staff = staff. Pay is visible to app_admin and
rota_admin (a Manager sees rates read-only); never to staff.
"""

from datetime import date, timedelta

import pytest

from app import db as db_module
from tests.conftest import TEST_PUB_ID, create_active_staff, login_as_person, login_as_pub

# Distinctive rates, so a hit can't be a coincidence (a time, a date, a count).
COLLEAGUE_RATE = 17.31
OWN_RATE = 14.29

# What the pay screens would show for the fixture: the rates, and the costs
# they produce. Each shift is 8 hours, so 138.48 / 114.32, and 252.80 for the
# day. A staff body containing any of these is a leak.
PAY_FIGURES = [b"17.31", b"14.29", b"138.48", b"114.32", b"252.80", b"252.8"]
POUND = "£".encode()

HUB_PERSON_ID = 9001

# Routes that carry pay or cost. A Staff session must not get a 200 from any
# of them; the Owner and a Manager must (the positive control — it proves the
# sweep below would see a leak if there were one).
PAY_ROUTES = [
    "/v/testvenue/rota/",                 # day-cost ticker on the rota grid
    "/v/testvenue/dashboard/",            # cost dashboard (week)
    "/v/testvenue/dashboard/month",       # cost dashboard (month)
    "/v/testvenue/payroll/",
    "/v/testvenue/payroll/export.csv",
    "/v/testvenue/payroll/export.pdf",
    "/v/testvenue/admin/staff",           # hourly rate column
]


def _set_rate(app, membership_id, rate):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE rota_staff_detail SET hourly_pay_rate = ? WHERE venue_membership_id = ?",
                     (rate, membership_id))
        conn.commit()


@pytest.fixture
def world(app, venue):
    """A venue with a colleague, a staff member, shifts with clock times for
    both (so predicted AND actual cost are non-zero), an open shift, a swap
    request and a leave request — enough that every staff screen has
    something to render."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())

    colleague_id, colleague_m, _ = create_active_staff(app, venue["id"], name="Carol Colleague",
                                                       role_id=venue["role_id"])
    staff_id, staff_m, _ = create_active_staff(app, venue["id"], name="Sam Staff",
                                               role_id=venue["role_id"])
    manager_id, manager_m, _ = create_active_staff(app, venue["id"], name="Mo Manager",
                                                   permission_level="rota_admin")
    _set_rate(app, colleague_m, COLLEAGUE_RATE)
    _set_rate(app, staff_m, OWN_RATE)
    _set_rate(app, manager_m, 0)

    with app.app_context():
        conn = db_module.get_db()
        shift_ids = {}
        for who, pid in (("colleague", colleague_id), ("staff", staff_id)):
            sid = conn.execute(
                """INSERT INTO shift (venue_id, person_id, venue_role_id, shift_date, start_time, end_time, status)
                   VALUES (?, ?, ?, ?, '09:00', '17:00', 'scheduled')""",
                (venue["id"], pid, venue["role_id"], monday.isoformat()),
            ).lastrowid
            conn.execute(
                "INSERT INTO attendance (shift_id, clock_in_at, clock_out_at) VALUES (?, ?, ?)",
                (sid, f"{monday.isoformat()} 09:00:00", f"{monday.isoformat()} 17:00:00"),
            )
            shift_ids[who] = sid
        # A future shift of the staff member's own, for the shift-detail page.
        shift_ids["staff_future"] = conn.execute(
            """INSERT INTO shift (venue_id, person_id, venue_role_id, shift_date, start_time, end_time, status)
               VALUES (?, ?, ?, ?, '18:00', '23:00', 'scheduled')""",
            (venue["id"], staff_id, venue["role_id"], (today + timedelta(days=2)).isoformat()),
        ).lastrowid
        shift_ids["open"] = conn.execute(
            """INSERT INTO shift (venue_id, person_id, venue_role_id, shift_date, start_time, end_time, status)
               VALUES (?, NULL, ?, ?, '12:00', '16:00', 'open')""",
            (venue["id"], venue["role_id"], (today + timedelta(days=3)).isoformat()),
        ).lastrowid
        conn.commit()

    return {
        "venue": venue, "today": today,
        "colleague_id": colleague_id, "colleague_m": colleague_m,
        "staff_id": staff_id, "staff_m": staff_m,
        "manager_id": manager_id, "manager_m": manager_m,
        "shift_ids": shift_ids,
    }


def _hub_staff_login(app, client, world, level="staff"):
    """A Hub-invited login: link the staff member to a Hub id and sign in
    the way the Hub does (person_id + the pub's pub_id, no local session)."""
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE person SET hub_person_id = ? WHERE id = ?", (HUB_PERSON_ID, world["staff_id"]))
        conn.execute("UPDATE app_access SET permission_level = ? WHERE venue_membership_id = ?",
                     (level, world["staff_m"]))
        conn.commit()
    with client.session_transaction() as sess:
        sess.clear()
        sess["pub_id"] = TEST_PUB_ID
        sess["person_id"] = HUB_PERSON_ID


def _concrete_urls(app, world, methods):
    """Every rule in the app with its arguments filled from the fixture.

    Ids are filled with the most revealing choice: the COLLEAGUE's membership
    and person (a staff session asking about somebody else) and the staff
    member's own shift (so the staff pages render rather than 404)."""
    s = world["shift_ids"]
    fill = {
        "slug": "testvenue",
        "membership_id": world["colleague_m"],
        "person_id": world["colleague_id"],
        "shift_id": s["staff_future"],
        "on_date": world["today"].isoformat(),
        "stored_filename": "nothing.jpg",
        "token": "not-a-real-token",
        # POST-only ids; nothing in the fixture has them, so these just reach
        # each route's not-found / refused path, which must not leak either.
        "swap_id": 1, "override_id": 1, "role_id": venue_role_id(world),
        "block_id": 1, "tag_id": 1, "access_id": 1, "leave_id": 1,
        "venue_id": world["venue"]["id"],
    }
    skip_endpoints = {"static", "dev_login", "billing_webhook"}
    out = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint in skip_endpoints or rule.rule.startswith("/internal/"):
            continue  # /internal/* is bearer-secret only; not a browser session's to call
        if not (set(methods) & rule.methods):
            continue
        if not set(rule.arguments) <= set(fill):
            raise AssertionError(f"New route argument(s) on {rule.rule} — add them to fill: "
                                 f"{set(rule.arguments) - set(fill)}")
        url = rule.rule
        for arg in rule.arguments:
            url = url.replace(f"<int:{arg}>", str(fill[arg])).replace(f"<path:{arg}>", str(fill[arg]))
            url = url.replace(f"<{arg}>", str(fill[arg]))
        out.append(url)
        # The open shift and the colleague's shift too, for the routes keyed on a shift.
        if "shift_id" in rule.arguments:
            for other in (s["open"], s["colleague"]):
                out.append(url.replace(f"/{fill['shift_id']}", f"/{other}"))
    return sorted(set(out))


def venue_role_id(world):
    return world["venue"]["role_id"]


def _leaks(body):
    return [fig.decode() for fig in PAY_FIGURES if fig in body]


def _query(world):
    """The query string every request carries, so a report or export that
    needs a date range (payroll export.csv answers 400 without one) renders
    its real content instead of failing early. Routes ignore what they don't
    read."""
    today = world["today"]
    monday = today - timedelta(days=today.weekday())
    return {"start": monday.isoformat(), "end": (monday + timedelta(days=6)).isoformat(),
            "week": monday.isoformat(), "month": monday.strftime("%Y-%m"),
            "person_id": world["colleague_id"]}


def _sweep(client, urls, method, query):
    """Hit every URL; return (urls that answered 200, {url: leaked figures})."""
    ok, leaks = [], {}
    for url in urls:
        resp = client.open(url, method=method, data={}, query_string=query)
        if resp.status_code == 200:
            ok.append(url)
        found = _leaks(resp.data)
        if found:
            leaks[url] = found
    return ok, leaks


@pytest.mark.parametrize("login", ["local", "hub"])
def test_a_staff_login_gets_no_pay_figures_from_any_get_route(app, client, world, login):
    if login == "local":
        login_as_person(client, world["staff_id"])
    else:
        _hub_staff_login(app, client, world)

    urls = _concrete_urls(app, world, {"GET"})
    ok, leaks = _sweep(client, urls, "GET", _query(world))

    assert leaks == {}, f"Staff saw pay figures: {leaks}"
    # Not vacuous: the staff portal really rendered for this session, so the
    # sweep was looking at real pages rather than a wall of redirects.
    for page in ("/v/testvenue/staff/", "/v/testvenue/staff/rota", "/v/testvenue/staff/leave",
                 "/v/testvenue/staff/open-shifts", "/v/testvenue/staff/swaps"):
        assert page in ok, f"{page} did not render for a {login} staff login"
    # And no staff-reachable page carries a £ sign at all — there is no money
    # a staff member is meant to see in RotaPulse.
    for url in ok:
        assert POUND not in client.get(url, query_string=_query(world)).data, f"£ on {url} for a staff login"


@pytest.mark.parametrize("login", ["local", "hub"])
def test_a_staff_login_is_refused_every_pay_route(app, client, world, login):
    if login == "local":
        login_as_person(client, world["staff_id"])
    else:
        _hub_staff_login(app, client, world)

    for url in PAY_ROUTES + [f"/v/testvenue/admin/staff/{world['colleague_m']}/edit"]:
        resp = client.get(url, query_string=_query(world))
        assert resp.status_code != 200, f"{url} answered 200 to a staff login"
        assert _leaks(resp.data) == []


def test_a_staff_login_gets_no_pay_figures_from_any_post_route(app, client, world):
    """POST responses too (a redirect body, an error page, any JSON), with an
    empty form. Run after the GETs above in a fresh fixture, so a POST that
    changes something can't hide a leak from them."""
    login_as_person(client, world["staff_id"])
    _ok, leaks = _sweep(client, _concrete_urls(app, world, {"POST"}), "POST", _query(world))
    assert leaks == {}, f"Staff saw pay figures: {leaks}"


def test_the_owner_sees_pay_on_every_pay_route(app, client, world):
    """Positive control: the figures the sweep hunts for really are on these
    pages for the people allowed them, so the sweep would catch a leak."""
    login_as_pub(client, TEST_PUB_ID)
    for url in PAY_ROUTES:
        assert client.get(url, query_string=_query(world)).status_code == 200, url
    assert b"17.31" in client.get("/v/testvenue/admin/staff").data
    assert b"252.80" in client.get("/v/testvenue/rota/").data  # the day-cost ticker
    assert b"138.48" in client.get("/v/testvenue/payroll/export.csv", query_string=_query(world)).data


def test_a_manager_sees_pay_read_only(app, client, world):
    login_as_person(client, world["manager_id"])
    for url in PAY_ROUTES:
        assert client.get(url, query_string=_query(world)).status_code == 200, url
    assert b"17.31" in client.get("/v/testvenue/admin/staff").data
    edit = client.get(f"/v/testvenue/admin/staff/{world['colleague_m']}/edit").data
    assert b"17.31" in edit
    assert b'name="hourly_pay_rate"' not in edit  # read-only: only the owner edits a rate


def test_hub_levels_map_to_rotapulse_tiers(app, client, world, monkeypatch):
    """The Hub's push decides the tier: manager -> rota_admin (sees pay),
    staff -> staff (does not). Nothing pushed can create app_admin."""
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "s3cret")
    auth = {"Authorization": "Bearer s3cret"}

    def push(person_id, level):
        return client.post("/internal/access", headers=auth, json={
            "pub_id": TEST_PUB_ID, "person_id": person_id, "name": f"Hub {level}",
            "email": f"hub-{level}@example.com", "level": level, "status": "active"})

    assert push(5001, "manager").status_code == 200
    assert push(5002, "staff").status_code == 200
    assert push(5003, "owner").status_code == 200  # not a level: lands as staff

    with app.app_context():
        rows = db_module.get_db().execute(
            """SELECT person.hub_person_id AS hub, app_access.permission_level AS level
               FROM person JOIN venue_membership ON venue_membership.person_id = person.id
               JOIN app_access ON app_access.venue_membership_id = venue_membership.id
               WHERE person.hub_person_id IN (5001, 5002, 5003)""").fetchall()
    assert {(r["hub"], r["level"]) for r in rows} == {(5001, "rota_admin"), (5002, "staff"), (5003, "staff")}

    def as_hub(person_id):
        with client.session_transaction() as sess:
            sess.clear()
            sess["pub_id"] = TEST_PUB_ID
            sess["person_id"] = person_id

    as_hub(5001)
    assert b"17.31" in client.get("/v/testvenue/admin/staff").data
    as_hub(5002)
    resp = client.get("/v/testvenue/admin/staff")
    assert resp.status_code != 200 and _leaks(resp.data) == []
