"""
Job roles — what a venue calls the jobs its staff do — and the difference
between retiring one and deleting it.

A role that has ever been used cannot be deleted. venue_membership,
shift and leave_block_role each hold a foreign key to venue_role, and the
connection runs with PRAGMA foreign_keys = ON (app/db.py), so the DELETE
raises IntegrityError. Two of those three were already guarded with a
friendly refusal; shift was not, and a landlord deleting a role that had
ever appeared on a rota got a 500 page instead. Historic shifts are never
tidied up, so within a few weeks of real use that is every role.

Archiving is the answer rather than cascading the delete or nulling the
shifts out. Nulling would lose which role a shift wanted, and on a FUTURE
open shift it does real damage: notify_open_shift (app/rota_grid.py) only
narrows the SMS to the right people `if shift_row["venue_role_id"]`, so a
nulled-out kitchen shift silently texts the whole venue. Archiving keeps
every historic link intact and simply stops the role being offered.

archived_at IS NULL means the role is in use. Nothing reads the timestamp
itself yet; it's a date rather than a flag because "when did we stop doing
food" is the question a landlord asks about it.
"""


def all_roles(db, venue_id):
    """Every role, archived or not — for the roles admin screen, which is
    the one place both belong."""
    return db.execute(
        "SELECT * FROM venue_role WHERE venue_id = ? ORDER BY name", (venue_id,)
    ).fetchall()


def active_roles(db, venue_id, include_ids=()):
    """The roles a form should offer.

    include_ids keeps a value that is already on the record being edited —
    a shift or a staff member set to a role that has since been archived.
    Leave it out and the <select> renders with nothing selected, so the
    next save of an unrelated field silently clears their role.
    """
    ids = [i for i in dict.fromkeys(include_ids) if i]
    extra = f" OR id IN ({','.join('?' * len(ids))})" if ids else ""
    return db.execute(
        f"""SELECT * FROM venue_role
            WHERE venue_id = ? AND (archived_at IS NULL{extra})
            ORDER BY name""",
        (venue_id, *ids),
    ).fetchall()


def shifts_using_role(db, venue_id, role_id):
    """How many shifts — past or future, rostered or open — name this role.
    Nonzero means the role can only be archived, not deleted."""
    return db.execute(
        "SELECT COUNT(*) AS n FROM shift WHERE venue_role_id = ? AND venue_id = ?",
        (role_id, venue_id),
    ).fetchone()["n"]


def staff_holding_role(db, venue_id, role_id):
    """How many staff records are set to this role. Counts people who have
    left as well as current staff: the foreign key doesn't care, and their
    membership row is what the DELETE would trip over."""
    return db.execute(
        "SELECT COUNT(*) AS n FROM venue_membership WHERE job_role_id = ? AND venue_id = ?",
        (role_id, venue_id),
    ).fetchone()["n"]


def blocks_using_role(db, venue_id, role_id):
    """How many blocked-date ranges are scoped to this role."""
    return db.execute(
        """SELECT COUNT(*) AS n FROM leave_block_role
           JOIN leave_block ON leave_block.id = leave_block_role.leave_block_id
           WHERE leave_block_role.venue_role_id = ? AND leave_block.venue_id = ?""",
        (role_id, venue_id),
    ).fetchone()["n"]


def role_usage(db, venue_id, role_id):
    """Everything that still points at this role — which is the same thing
    as everything the DELETE would trip over. All zero means it was never
    used and can genuinely be deleted; anything else means archive."""
    return {
        "staff": staff_holding_role(db, venue_id, role_id),
        "shifts": shifts_using_role(db, venue_id, role_id),
        "blocks": blocks_using_role(db, venue_id, role_id),
    }
