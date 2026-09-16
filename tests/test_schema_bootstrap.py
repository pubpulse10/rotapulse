"""
The schema reaches the deployment.

Every migration this app has lives in `db.init_schema()` — the CREATE TABLE
IF NOT EXISTS block and every `_add_column_if_missing` call. Until 2026-09-16
nothing in the running app called it: not `wsgi.py`, not `create_app()`, not
the Dockerfile or `docker-entrypoint.sh`. The only callers were
`scripts/init_db.py`, a developer's script, and this test suite.

So a new column landed in the CODE on push and never in the live DATABASE,
and `/health` could not show it because it reports an environment variable
rather than anything the database knows. pubpulse-hub already did this in
`create_app()` and pricepulse in `wsgi.py`; RotaPulse was the outlier.

These two tests are the tripwire. If somebody removes that call again, the
symptom is a production outage on whichever screen reads the new column, so
it is worth failing loudly here instead.
"""

import sqlite3

from app import create_app, db as db_module


def _tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def _columns(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_creating_the_app_creates_the_schema(monkeypatch, tmp_path):
    """A brand-new deployment, or a disaster-recovery restore onto an empty
    disk, must not need a human to remember a script."""
    db_path = tmp_path / "fresh.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)

    create_app()

    tables = _tables(db_path)
    assert "venue" in tables
    assert "leave_request" in tables
    assert "leave_block" in tables


def test_an_added_column_reaches_a_database_that_predates_it(monkeypatch, tmp_path):
    """The failure mode that actually bit: an EXISTING live database, created
    before the column was written, which every deploy quietly left behind."""
    db_path = tmp_path / "old.db"
    old = sqlite3.connect(db_path)
    # A venue_settings from before step 2 of docs/leave-design.md.
    old.execute("CREATE TABLE venue_settings (venue_id INTEGER PRIMARY KEY, target_staff_cost_percent NUMERIC)")
    old.commit()
    old.close()
    assert "full_time_allowance_days" not in _columns(db_path, "venue_settings")
    monkeypatch.setattr(db_module, "DB_PATH", db_path)

    create_app()

    # leave.allowance_for() reads this off the row; without it a sqlite3.Row
    # raises IndexError and every staff member's Leave page is a 500.
    assert "full_time_allowance_days" in _columns(db_path, "venue_settings")
