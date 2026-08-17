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

from app.db import get_connection, delete_venue_by_pub_id

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
        name_upper = (r["name"] or "").strip().upper()
        # Substring, not equality: the venue is named e.g. "The Cock, Dereham"
        # in RotaPulse, which never equalled "THE COCK" — so this
        # belt-and-braces guard silently did nothing and protection rested
        # entirely on the pub_id check.
        is_keep = r["pub_id"] in KEEP_PUB_IDS or any(k in name_upper for k in KEEP_NAMES)
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

    deleted = {}
    for _, _, pub_id in targets:
        for table, count in delete_venue_by_pub_id(conn, pub_id).items():
            deleted[table] = deleted.get(table, 0) + count

    print("\nDeleted:")
    for t, n in sorted(deleted.items()):
        print(f"   {t}: {n}")
    print("Done. THE COCK kept.")
    conn.close()


if __name__ == "__main__":
    main()
