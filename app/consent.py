"""
Right-to-erasure for a departed staff member's sensitive data (spec §3's
own instruction: "consent, storage, and right-to-erasure need proper
design from day one, not retrofitting").

Policy (user-confirmed): erase identity fields — name is kept for historic
shift/payroll readability, but home address, phone, email, avatar and any
attendance photos are deleted — while keeping the anonymised hours-worked/
gross-pay figures already handed to payroll, matching typical UK accounting
retention norms. shift/attendance rows themselves are never deleted.

Precondition: only a membership that has already been marked 'left' can be
erased — erasing an active staff member's login-critical data out from
under them would break their ability to use the app they're still using.
"""

from app.db import get_db
from app.media import delete_file


def erase_person_sensitive_data(venue_id: int, membership_id: int) -> bool:
    db = get_db()
    membership = db.execute(
        "SELECT * FROM venue_membership WHERE id = ? AND venue_id = ?", (membership_id, venue_id)
    ).fetchone()
    if membership is None or membership["status"] != "left":
        return False

    person = db.execute("SELECT * FROM person WHERE id = ?", (membership["person_id"],)).fetchone()
    if person is None:
        return False

    if person["avatar_url"]:
        delete_file("avatar", person["avatar_url"])

    photo_rows = db.execute(
        """SELECT attendance.photo_url FROM attendance
           JOIN shift ON shift.id = attendance.shift_id
           WHERE shift.person_id = ? AND attendance.photo_url IS NOT NULL""",
        (person["id"],),
    ).fetchall()
    for row in photo_rows:
        delete_file("attendance_photo", row["photo_url"])
    db.execute(
        """UPDATE attendance SET photo_url = NULL WHERE shift_id IN
           (SELECT id FROM shift WHERE person_id = ?)""",
        (person["id"],),
    )

    db.execute(
        """UPDATE person SET mobile = NULL, email = NULL,
           avatar_url = NULL, date_of_birth = NULL, erased_at = datetime('now')
           WHERE id = ?""",
        (person["id"],),
    )
    db.execute(
        "UPDATE rota_staff_detail SET home_address = NULL WHERE venue_membership_id = ?",
        (membership_id,),
    )
    db.commit()
    return True
