"""
Recover ONE customer's deleted data, without touching anybody else's.

Run this in the affected app's Render Shell:

    python -m scripts.recover_pub --pub 8 --at 2026-09-05T18:00:00Z
    python -m scripts.recover_pub --pub 8 --at 2026-09-05T18:00:00Z --apply

The first form changes nothing: it restores a point-in-time copy beside the
live database, works out what that customer had then and does not have now,
and prints it. The second form inserts those rows back.

WHY THIS EXISTS
Every app in this family keeps ONE SQLite database shared by every customer.
So the documented disaster-recovery procedure — delete the file, restart, let
the entrypoint pull the latest snapshot — is right for a dead disk and badly
wrong for "one landlord deleted their rota". It would roll every other customer
back to the same moment, destroying their work to fix one person's, and would
not even recover the deleted rows, because the latest snapshot already contains
the deletion.

This does the surgical version instead.

WHAT IT WILL AND WILL NOT DO
  - It only ever INSERTs. It never updates or deletes a live row, so it cannot
    destroy work done since the loss.
  - It only touches rows belonging to the pub you name, reached by following
    declared foreign keys down from that customer's own rows.
  - Rows that exist in both but DIFFER are reported and left alone. A changed
    row is not a deleted row, and quietly overwriting one would be the script
    causing the data loss it was called in to fix.
  - Tables it could not connect to a customer are listed explicitly. Nothing
    is silently unexamined: if the schema ever gains a table with no path back
    to a pub, you find out here rather than by a customer noticing.

Stdlib only, and it imports nothing from the app, so the same file works
unchanged in all five services.
"""

import argparse
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict, deque
from pathlib import Path

LITESTREAM_CONFIG = "/etc/litestream.yml"

# Columns that mean "this row belongs to a customer". PricePulse hangs data
# straight off pub_id; RotaPulse, TaskPulse and DiaryPulse put a venue table in
# between and scope most things by venue_id. Everything deeper is reached by
# following foreign keys, not by guessing at column names.
ROOT_COLUMN = "pub_id"


def db_path_from_config(config_path):
    """The database Litestream is replicating, read from its own config.

    Read rather than passed in, so this script needs no per-app knowledge and
    cannot be pointed at the wrong file by a typo under pressure.
    """
    text = Path(config_path).read_text(encoding="utf-8")
    m = re.search(r"^\s*-\s*path:\s*(\S+)", text, re.M)
    if not m:
        raise SystemExit(f"No database path found in {config_path}")
    return m.group(1)


def restore(config_path, db_path, timestamp, dest):
    """Point-in-time restore to a side file. Never touches the live database."""
    cmd = ["litestream", "restore", "-config", config_path, "-o", str(dest)]
    if timestamp:
        cmd += ["-timestamp", timestamp]
    cmd.append(db_path)
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            "litestream restore failed:\n"
            f"{result.stderr.strip() or result.stdout.strip()}\n\n"
            "If it complains about -timestamp, run `litestream restore -h` and "
            "check the flag name for this version."
        )
    if not Path(dest).exists():
        raise SystemExit("litestream reported success but wrote no file.")


def connect(path, read_only=False):
    if read_only:
        # immutable, not mode=ro: these databases run in WAL mode, and mode=ro
        # creates -shm/-wal files beside the one it opens.
        return sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    return sqlite3.connect(path)


def tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def columns(conn, table):
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def primary_key(conn, table):
    pk = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")') if r[5]]
    return pk[0] if len(pk) == 1 else None


def foreign_keys(conn, table):
    """[(this_column, parent_table, parent_column)] for one table."""
    out = []
    for r in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
        # (id, seq, table, from, to, on_update, on_delete, match)
        out.append((r[3], r[2], r[4] or "id"))
    return out


def reachable_rows(conn, pub_id):
    """Every row belonging to one customer, as {table: {pk: row_dict}}.

    Seeded from tables carrying pub_id directly, then widened by following
    declared foreign keys until nothing new is found. That closure is what
    picks up a TaskPulse checklist_task_result, three tables below the venue,
    without this script needing to know TaskPulse's schema.
    """
    conn.row_factory = sqlite3.Row
    all_tables = tables(conn)

    # parent table -> [(child table, child column, parent column)]
    children = defaultdict(list)
    for t in all_tables:
        for col, parent, parent_col in foreign_keys(conn, t):
            children[parent].append((t, col, parent_col))

    found = defaultdict(dict)
    queue = deque()

    for t in all_tables:
        if ROOT_COLUMN not in columns(conn, t):
            continue
        pk = primary_key(conn, t)
        rows = conn.execute(
            f'SELECT * FROM "{t}" WHERE {ROOT_COLUMN} = ?', (pub_id,)).fetchall()
        for row in rows:
            key = row[pk] if pk else tuple(row)
            found[t][key] = dict(row)
        if rows:
            queue.append(t)

    while queue:
        parent = queue.popleft()
        parent_pk = primary_key(conn, parent)
        if not parent_pk:
            continue
        parent_ids = {r[parent_pk] for r in found[parent].values()
                      if parent_pk in r}
        if not parent_ids:
            continue
        for child, child_col, parent_col in children.get(parent, []):
            if parent_col != parent_pk:
                continue
            child_pk = primary_key(conn, child)
            before = len(found[child])
            # Chunked, because SQLite caps how many variables one statement
            # may bind and a busy venue can exceed it.
            ids = list(parent_ids)
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f'SELECT * FROM "{child}" WHERE "{child_col}" IN ({marks})',
                    chunk).fetchall()
                for row in rows:
                    key = row[child_pk] if child_pk else tuple(row)
                    found[child][key] = dict(row)
            if len(found[child]) > before:
                queue.append(child)

    return found, all_tables


def compare(backup_rows, live_rows):
    """(missing, changed) per table."""
    missing, changed = defaultdict(list), defaultdict(list)
    for table, rows in backup_rows.items():
        live = live_rows.get(table, {})
        for key, row in rows.items():
            if key not in live:
                missing[table].append(row)
            elif dict(live[key]) != dict(row):
                changed[table].append(key)
    return missing, changed


def insertion_order(conn, table_names):
    """Parents before children, so a re-inserted row's references exist."""
    deps = {t: {p for _c, p, _pc in foreign_keys(conn, t) if p != t}
            for t in table_names}
    ordered, seen = [], set()

    def visit(t, trail=()):
        if t in seen or t not in deps:
            return
        if t in trail:
            return          # a cycle; the caller's transaction will catch any breakage
        for parent in deps[t]:
            visit(parent, trail + (t,))
        if t not in seen:
            seen.add(t)
            ordered.append(t)

    for t in table_names:
        visit(t)
    return ordered


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--pub", type=int, required=True, help="the pub_id to recover")
    ap.add_argument("--at", help="RFC3339 timestamp to restore from, e.g. "
                                 "2026-09-05T18:00:00Z. Omit for the latest backup.")
    ap.add_argument("--config", default=LITESTREAM_CONFIG)
    ap.add_argument("--db", help="live database (default: read from the litestream config)")
    ap.add_argument("--restored", help="use this already-restored file instead of "
                                       "running litestream")
    ap.add_argument("--apply", action="store_true",
                    help="actually insert the missing rows (default: report only)")
    args = ap.parse_args(argv)

    live_path = args.db or db_path_from_config(args.config)
    if not Path(live_path).exists():
        raise SystemExit(f"Live database not found: {live_path}")

    workdir = Path(tempfile.mkdtemp(prefix="recover-"))
    try:
        if args.restored:
            backup_path = Path(args.restored)
        else:
            backup_path = workdir / "backup.db"
            print(f"Restoring {live_path} as at {args.at or 'the latest backup'}:")
            restore(args.config, live_path, args.at, backup_path)

        backup = connect(backup_path, read_only=True)
        # integrity_check RAISES on a badly damaged file rather than returning
        # a string — a truncated or scribbled database throws DatabaseError
        # from this line. Both paths have to end in a plain message: whoever is
        # running this is mid-incident and does not need a traceback.
        try:
            integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise SystemExit(
                f"The restored copy will not open: {exc}. "
                "Try an earlier --at timestamp, and check the Backups tiles "
                "on the Hub's health board.")
        if integrity != "ok":
            raise SystemExit(f"The restored copy is not sound: {integrity[:120]}")

        backup_rows, backup_tables = reachable_rows(backup, args.pub)
        if not any(backup_rows.values()):
            raise SystemExit(
                f"No rows for pub {args.pub} in the backup. Check the pub_id, and "
                f"that you are running this in the right app.")

        live = connect(live_path)
        live_rows, _ = reachable_rows(live, args.pub)

        missing, changed = compare(backup_rows, live_rows)

        print(f"\nPub {args.pub}: comparing the backup against the live database\n")
        total = 0
        for table in sorted(set(backup_rows) | set(live_rows)):
            b, l = len(backup_rows.get(table, {})), len(live_rows.get(table, {}))
            gone = len(missing.get(table, []))
            total += gone
            if b or l:
                flag = f"  <-- {gone} MISSING" if gone else ""
                print(f"  {table:32} backup {b:>6}   live {l:>6}{flag}")

        if changed:
            print("\n  Rows present in both but DIFFERENT (left alone — a changed "
                  "row is not a deleted one):")
            for table, keys in changed.items():
                print(f"    {table}: {len(keys)} row(s)")

        unreached = [t for t in backup_tables
                     if t not in backup_rows and t not in live_rows]
        if unreached:
            print("\n  Tables with no path back to a customer, so NOT examined.")
            print("  Check these by hand if the missing data is not listed above:")
            print("    " + ", ".join(unreached))

        if not total:
            print("\nNothing is missing for this customer. Whatever they are "
                  "describing is not deleted rows — check the app's own filters "
                  "(venue, date range) before restoring anything.")
            return 0

        if not args.apply:
            print(f"\n{total} row(s) would be restored. Nothing has been changed.")
            print("Re-run with --apply to insert them.")
            return 0

        # Copy the live database before writing to it. If this goes wrong, the
        # state you started from is still on disk.
        safety = Path(live_path).with_suffix(".before-recovery")
        shutil.copy2(live_path, safety)
        print(f"\nLive database copied to {safety}")

        order = insertion_order(backup, list(missing))
        inserted = 0
        try:
            live.execute("BEGIN")
            for table in order:
                for row in missing.get(table, []):
                    cols = ", ".join(f'"{c}"' for c in row)
                    marks = ", ".join("?" * len(row))
                    live.execute(f'INSERT INTO "{table}" ({cols}) VALUES ({marks})',
                                 list(row.values()))
                    inserted += 1
            live.commit()
        except Exception as exc:
            live.rollback()
            raise SystemExit(
                f"Insert failed, so NOTHING was changed: {exc}\n"
                f"The live database is untouched; a copy is at {safety}.")

        print(f"Restored {inserted} row(s) for pub {args.pub}.")
        print("Ask the customer to confirm before closing this off.")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
