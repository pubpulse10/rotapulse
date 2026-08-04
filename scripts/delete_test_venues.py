"""
One-off cleanup — delete TEST venues, keeping THE COCK.

    python scripts/delete_test_venues.py            # PREVIEW — shows what would go, deletes NOTHING
    python scripts/delete_test_venues.py --confirm  # actually deletes

Keeps any venue whose pub_id is in KEEP_PUB_IDS or whose name is 'THE COCK';
deletes every other venue plus its child rows across all venue-scoped tables
(and the venue_membership-chain children) so nothing is left orphaned.

App-local: touches only THIS app's database — never the pub's shared PubPulse
identity or any other app. Always preview first, confirm THE COCK is listed
under KEEP, then re-run with --confirm.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_connection

KEEP_PUB_IDS = {2}          # THE COCK's shared PubPulse pub_id
KEEP_NAMES = {"THE COCK"}   # belt-and-braces: keep by name too


def main():
    confirm = "--confirm" in sys.argv
    conn = get_connection()
    conn.execute("PRAGMA foreign_keys = OFF")

    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    vt = "venues" if "venues" in tables else "venue"

    keep, targets = [], []
    for r in conn.execute(f"SELECT id, name, pub_id FROM {vt} ORDER BY pub_id"):
        is_keep = r["pub_id"] in KEEP_PUB_IDS or (r["name"] or "").strip().upper() in KEEP_NAMES
        (keep if is_keep else targets).append((r["id"], r["name"], r["pub_id"]))

    def show(label, items):
        print(label)
        for vid, name, pub_id in items or [("-", "(none)", "-")]:
            print(f"   venue {vid}  ·  pub {pub_id}  ·  {name}")

    print(f"\nVenue table: {vt}")
    show("KEEP:", keep)
    show("DELETING:" if confirm else "WOULD DELETE:", targets)

    if not targets:
        print("\nNothing to delete.")
        conn.close()
        return
    if not confirm:
        print("\nPREVIEW ONLY — nothing deleted. Check THE COCK is under KEEP, then re-run with --confirm.")
        conn.close()
        return

    ids = [vid for vid, _, _ in targets]
    ph = ",".join("?" * len(ids))
    deleted = {}

    # venue_membership-chain children (RotaPulse): app_access / rota_staff_detail
    # hang off venue_membership, not venue_id — clear them first.
    if "venue_membership" in tables:
        mem = [r["id"] for r in conn.execute(
            f"SELECT id FROM venue_membership WHERE venue_id IN ({ph})", ids)]
        if mem:
            mph = ",".join("?" * len(mem))
            for child in ("app_access", "rota_staff_detail"):
                if child in tables:
                    c = conn.execute(f"DELETE FROM {child} WHERE venue_membership_id IN ({mph})", mem)
                    if c.rowcount:
                        deleted[child] = c.rowcount

    for t in tables:
        if t == vt:
            continue
        colnames = {c["name"] for c in conn.execute(f"PRAGMA table_info({t})")}
        if "venue_id" in colnames:
            c = conn.execute(f"DELETE FROM {t} WHERE venue_id IN ({ph})", ids)
            if c.rowcount:
                deleted[t] = c.rowcount
    c = conn.execute(f"DELETE FROM {vt} WHERE id IN ({ph})", ids)
    deleted[vt] = c.rowcount
    conn.commit()

    print("\nDeleted:")
    for t, n in sorted(deleted.items()):
        print(f"   {t}: {n}")
    print("Done. THE COCK kept.")
    conn.close()


if __name__ == "__main__":
    main()
