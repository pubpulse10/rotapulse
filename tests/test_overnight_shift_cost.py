"""
Overnight shifts in the predicted wage bill.

A pub's late shift is routinely 20:00-02:00. predicted_cost() worked out the
length by subtracting clock times, giving -18 hours, then clamped the negative
to zero with max(hours, 0) — so every overnight shift added exactly nothing to
the predicted labour cost. Silent, and wrong in the reassuring direction: the
figure on the rota week view read lower than the pub's real commitment.

actual_cost() was never affected, because it works from full clock-in and
clock-out timestamps. So the two figures disagreed for any venue running late
shifts, with nothing on screen to explain the gap.

Found in the 2026-09-04 estate sweep.
"""

import pytest

from app.costs import predicted_cost, shift_hours
from app import db as db_module
from tests.conftest import create_active_staff


# --------------------------------------------------------------------------- #
# The arithmetic
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("start,end,expected", [
    ("09:00", "17:00", 8.0),        # ordinary day shift
    ("20:00", "02:00", 6.0),        # the one that was costed at zero
    ("22:30", "03:15", 4.75),       # overnight, not on the hour
    ("23:00", "00:00", 1.0),        # ends exactly at midnight
    ("00:00", "08:00", 8.0),        # starts exactly at midnight
    ("12:00", "12:00", 0.0),        # ad-hoc placeholder, NOT 24 hours
])
def test_shift_length(start, end, expected):
    assert shift_hours(start, end) == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# ...and through the real cost figure
# --------------------------------------------------------------------------- #

def _rostered(app, venue_id, start, end, rate=12.0):
    """One scheduled shift for one staff member on a known pay rate."""
    person_id, membership_id, _ = create_active_staff(app, venue_id)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_staff_detail SET hourly_pay_rate = ? WHERE venue_membership_id = ?",
            (rate, membership_id),
        )
        conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) "
            "VALUES (?, ?, '2026-09-10', ?, ?, 'scheduled')",
            (venue_id, person_id, start, end),
        )
        conn.commit()


def test_an_overnight_shift_is_no_longer_costed_at_zero(app, venue):
    _rostered(app, venue["id"], "20:00", "02:00", rate=12.0)
    with app.app_context():
        assert predicted_cost(venue["id"], "2026-09-10", "2026-09-10") == pytest.approx(72.0)


def test_a_day_shift_is_unchanged(app, venue):
    """The fix must not move any figure that was already right."""
    _rostered(app, venue["id"], "09:00", "17:00", rate=12.0)
    with app.app_context():
        assert predicted_cost(venue["id"], "2026-09-10", "2026-09-10") == pytest.approx(96.0)
