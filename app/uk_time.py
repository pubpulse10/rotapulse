"""
Single source of truth for "the current UK wall-clock moment" — this app
is UK-pub-only (spec), so there's deliberately no per-venue timezone
concept anywhere else: shift_date/start_time/end_time, an admin typing
"17:00" into the rota, are all already implicitly UK local time.

Real report, 2026-08-19: a staff member's ad-hoc clock-in recorded a time
an hour behind UK time. Root cause — the server runs in UTC (Render's/
Docker's default; no TZ env var or system tzdata override anywhere), but
datetime.now() and SQLite's datetime('now')/strftime(...,'now') are BOTH
always UTC with no timezone awareness at all. Anywhere one of those was
compared against or stored alongside a UK-local wall-clock value was
silently off by the UK's current DST offset — 0 hours in winter (GMT), 1
hour in summer (BST) — which is exactly why this shipped without being
noticed for months and only became obvious in August. This was already
flagged as a known risk in scripts/check_shift_notifications.py's own
docstring ("worth revisiting... across a BST/GMT change on a UTC server")
before it became a real report.

uk_now()/uk_today()/uk_now_iso() give NAIVE values (tzinfo stripped after
conversion) representing UK local wall-clock time — deliberately naive,
not aware, so they're drop-in replacements for the naive datetime.now()/
date.today() calls throughout the app and need no changes to how those
values are later parsed/compared/stored (they were always naive strings
already, e.g. shift.start_time). Needs the tzdata package (requirements.txt)
since zoneinfo has no IANA database of its own on Windows or a slim Docker
image — confirmed locally: ZoneInfo("Europe/London") raises
ZoneInfoNotFoundError without it.

Deliberately NOT applied to created_at/invited_at/accepted_at/decided_at-
style audit-trail timestamps elsewhere (mostly SQLite CREATE TABLE DEFAULT
(datetime('now')) clauses, which can't call into Python at all) — those are
used for ordering/existence checks where the timezone genuinely doesn't
matter, not compared against a UK-local wall-clock value the way shift
times and attendance are. Revisit only if one of those specifically starts
being shown to a user as "the time" and the same hour-off symptom shows up
there too.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

_UK_TZ = ZoneInfo("Europe/London")


def uk_now() -> datetime:
    """Current UK wall-clock time, naive (no tzinfo) — correctly BST-aware
    (unlike a hardcoded UTC+1), so this is right in both summer and winter
    without the caller needing to know or check which one it currently is."""
    return datetime.now(_UK_TZ).replace(tzinfo=None)


def uk_today() -> date:
    return uk_now().date()


def uk_now_iso() -> str:
    """Matches the "YYYY-MM-DD HH:MM:SS" shape SQLite's own datetime('now')
    produces, for a straight drop-in wherever a column previously relied on
    that (see app/staff_portal.py's attendance/shift timestamp writes)."""
    return uk_now().strftime("%Y-%m-%d %H:%M:%S")
