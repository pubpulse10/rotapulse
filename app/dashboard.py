"""
Cost dashboard (spec §11, revised for week-level rather than day-level
turnover scrutiny) and the birthday/anniversary differentiator (spec §10 —
event tags and the weather strip live on the rota grid itself,
app/rota_grid.py; the weekly digest composer lives in app/digest.py).

Turnover is entered once per week (weekly_turnover), not once per day —
staff cost (predicted from the rota, actual from clock-in/out) is still
computed live at whatever date range is needed (app/costs.py), so a week's
"% of turnover spent on staff" is always a live figure even though the
turnover half is a manually-entered weekly number. The month view is the
main place this is meant to be used from: every week touching that month,
each with its own predicted/actual turnover entry right in the row, plus a
monthly total — one screen to update a whole month's predicted turnover in.
A week is assigned to whichever calendar month its Monday falls in; a week
straddling month-end counts wholly against the earlier month rather than
being split day-by-day, the same "don't over-scrutinise this" reasoning
that moved turnover entry from daily to weekly in the first place.
"""

from datetime import date, timedelta

import flask

from app.costs import actual_cost, predicted_cost
from app.db import get_db
from app.rota_auth import register_identity, require_permission
from app.venue_scope import register_venue_gate, register_venue_scope

dashboard_bp = flask.Blueprint("dashboard", __name__, url_prefix="/v/<slug>/dashboard")
register_venue_scope(dashboard_bp)
register_venue_gate(dashboard_bp)
register_identity(dashboard_bp)


def _monday_of(on_date: date) -> date:
    return on_date - timedelta(days=on_date.weekday())


def _pct(numerator, denominator):
    if not denominator:
        return None
    return round(numerator / denominator * 100, 1)


def _week_summary(db, venue_id, week_start, target_pct):
    """One week's predicted/actual staff cost, turnover, and % of turnover
    spent on staff — the same shape used on both the week and month views,
    computed fresh every time (spec §11: no freeze/snapshot step)."""
    week_end = week_start + timedelta(days=6)
    turnover = db.execute(
        "SELECT * FROM weekly_turnover WHERE venue_id = ? AND week_start_date = ?",
        (venue_id, week_start.isoformat()),
    ).fetchone()
    predicted_turnover = turnover["predicted_amount"] if turnover else None
    actual_turnover = turnover["actual_amount"] if turnover else None
    p_cost = predicted_cost(venue_id, week_start.isoformat(), week_end.isoformat())
    a_cost = actual_cost(venue_id, week_start.isoformat(), week_end.isoformat())
    predicted_pct = _pct(p_cost, predicted_turnover)
    actual_pct = _pct(a_cost, actual_turnover)
    return {
        "week_start": week_start,
        "week_end": week_end,
        "predicted_turnover": predicted_turnover,
        "actual_turnover": actual_turnover,
        "predicted_cost": p_cost,
        "actual_cost": a_cost,
        "predicted_pct": predicted_pct,
        "actual_pct": actual_pct,
        "on_track": (actual_pct <= target_pct) if (actual_pct is not None and target_pct) else None,
    }


@dashboard_bp.route("/")
@require_permission("app_admin", "rota_admin")
def week():
    db = get_db()
    venue = flask.g.venue
    week_param = flask.request.args.get("week")
    week_start = _monday_of(date.fromisoformat(week_param)) if week_param else _monday_of(date.today())

    settings = db.execute("SELECT * FROM venue_settings WHERE venue_id = ?", (venue["id"],)).fetchone()
    target_pct = settings["target_staff_cost_percent"] if settings else None

    summary = _week_summary(db, venue["id"], week_start, target_pct)

    return flask.render_template(
        "dashboard/week.html",
        venue=venue,
        summary=summary,
        target_pct=target_pct,
        prev_week=(week_start - timedelta(days=7)).isoformat(),
        next_week=(week_start + timedelta(days=7)).isoformat(),
    )


@dashboard_bp.route("/turnover", methods=["POST"])
@require_permission("app_admin", "rota_admin")
def update_weekly_turnover():
    db = get_db()
    form = flask.request.form
    week_start_date = form["week_start_date"]
    predicted = form.get("predicted_amount", type=float)
    actual = form.get("actual_amount", type=float)
    db.execute(
        """INSERT INTO weekly_turnover (venue_id, week_start_date, predicted_amount, actual_amount)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(venue_id, week_start_date) DO UPDATE SET
           predicted_amount = COALESCE(?, weekly_turnover.predicted_amount),
           actual_amount = COALESCE(?, weekly_turnover.actual_amount),
           updated_at = datetime('now')""",
        (flask.g.venue["id"], week_start_date, predicted, actual, predicted, actual),
    )
    db.commit()
    month_param = form.get("month")
    if month_param:
        return flask.redirect(flask.url_for("dashboard.month", month=month_param))
    return flask.redirect(flask.url_for("dashboard.week", week=week_start_date))


@dashboard_bp.route("/month")
@require_permission("app_admin", "rota_admin")
def month():
    db = get_db()
    venue = flask.g.venue
    month_param = flask.request.args.get("month")  # YYYY-MM
    if month_param:
        year, mon = map(int, month_param.split("-"))
    else:
        today = date.today()
        year, mon = today.year, today.month

    first_day = date(year, mon, 1)
    next_month_first = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    last_day = next_month_first - timedelta(days=1)
    prev_month_first = date(year - 1, 12, 1) if mon == 1 else date(year, mon - 1, 1)

    settings = db.execute("SELECT * FROM venue_settings WHERE venue_id = ?", (venue["id"],)).fetchone()
    target_pct = settings["target_staff_cost_percent"] if settings else None

    weeks = []
    monday = _monday_of(first_day)
    if monday < first_day:
        monday += timedelta(days=7)
    while monday <= last_day:
        weeks.append(_week_summary(db, venue["id"], monday, target_pct))
        monday += timedelta(days=7)

    total_predicted_turnover = sum(w["predicted_turnover"] or 0 for w in weeks)
    total_actual_turnover = sum(w["actual_turnover"] or 0 for w in weeks)
    total_predicted_cost = sum(w["predicted_cost"] for w in weeks)
    total_actual_cost = sum(w["actual_cost"] for w in weeks)

    return flask.render_template(
        "dashboard/month.html",
        venue=venue,
        month_value=first_day.strftime("%Y-%m"),
        month_label=first_day.strftime("%B %Y"),
        weeks=weeks,
        target_pct=target_pct,
        total_predicted_turnover=total_predicted_turnover,
        total_actual_turnover=total_actual_turnover,
        total_predicted_cost=total_predicted_cost,
        total_actual_cost=total_actual_cost,
        total_predicted_pct=_pct(total_predicted_cost, total_predicted_turnover),
        total_actual_pct=_pct(total_actual_cost, total_actual_turnover),
        prev_month=prev_month_first.strftime("%Y-%m"),
        next_month=next_month_first.strftime("%Y-%m"),
    )


@dashboard_bp.route("/birthdays")
@require_permission("app_admin", "rota_admin")
def birthdays():
    db = get_db()
    rows = db.execute(
        """SELECT person.name, person.date_of_birth FROM person
           JOIN venue_membership ON venue_membership.person_id = person.id
           WHERE venue_membership.venue_id = ? AND venue_membership.status = 'active'
           AND person.date_of_birth IS NOT NULL""",
        (flask.g.venue["id"],),
    ).fetchall()

    today = date.today()

    def days_until_next(dob_str):
        month, day = int(dob_str[5:7]), int(dob_str[8:10])
        this_year = date(today.year, month, day) if _valid_date(today.year, month, day) else date(today.year, 3, 1)
        if this_year < today:
            this_year = date(today.year + 1, month, day)
        return (this_year - today).days

    def _valid_date(year, month, day):
        try:
            date(year, month, day)
            return True
        except ValueError:
            return False

    upcoming = sorted(
        ({"name": r["name"], "date_of_birth": r["date_of_birth"], "days_until": days_until_next(r["date_of_birth"])} for r in rows),
        key=lambda x: x["days_until"],
    )
    return flask.render_template("dashboard/birthdays.html", upcoming=upcoming)
