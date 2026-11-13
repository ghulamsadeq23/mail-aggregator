"""Re-import InstAddr mail after a parser fix. Stop the app before running.

    python tools/instaddr_reimport.py            (all InstAddr accounts)
    python tools/instaddr_reimport.py --yes      (skip the confirmation)

Messages are parsed once, when they are fetched, so mail stored by an older
parser keeps its old HTML. This deletes the stored InstAddr messages and the
"already seen" markers; on the next start the worker fetches the newest
instaddr.initial_fetch messages of every linked account again with the
current parser. Linked sessions are kept. Gmail/Outlook mail is untouched.
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import ConfigStore  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.environ.get("MAILAGG_CONFIG", "config.json"))
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    store = ConfigStore(args.config)
    db_path = store.get("database_path", default="emails.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(store.path), db_path)
    if not os.path.exists(db_path):
        sys.exit("Database not found: %s" % db_path)

    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM emails WHERE source_platform = 'InstAddr'").fetchone()[0]
    print("%d stored InstAddr message(s) in %s" % (count, db_path))
    print("They will be deleted and the newest %s per account fetched again."
          % store.get("instaddr", "initial_fetch", default=20))
    if not args.yes and input("Type yes to continue: ").strip().lower() != "yes":
        sys.exit("Cancelled.")

    conn.execute("DELETE FROM emails WHERE source_platform = 'InstAddr'")
    conn.execute("DELETE FROM instaddr_seen")
    conn.execute("DELETE FROM sync_state WHERE key LIKE 'instaddr:%'")
    conn.commit()
    conn.close()
    print("Done. Start the app; raise instaddr.initial_fetch first to re-import more.")


if __name__ == "__main__":
    main()
