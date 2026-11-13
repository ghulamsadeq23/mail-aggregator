"""Check an InstAddr session and dump what the poller sees.

    python tools/instaddr_debug.py --session-key <cookie_sessionhash> \
        [--csrf-token <cookie_csrf_token>] [--dump-dir debug_out]

Prints the messages found in the inbox list and the parsed fields of
the newest one. With --dump-dir the raw HTML responses are saved, which
is what you need to adjust instaddr.list_regex or the parser if the
site markup changes.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import DEFAULT_CONFIG  # noqa: E402
from app.instaddr_poller import (InstAddrClient, parse_inbox_list,  # noqa: E402
                                 parse_message_view)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session-key", required=True)
    parser.add_argument("--csrf-token", default="")
    parser.add_argument("--base-url", default=DEFAULT_CONFIG["instaddr"]["base_url"])
    parser.add_argument("--list-regex", default="")
    parser.add_argument("--dump-dir", default="")
    args = parser.parse_args()

    client = InstAddrClient(args.base_url, args.session_key, args.csrf_token,
                            DEFAULT_CONFIG["instaddr"]["user_agent"])
    list_html = client.fetch_list_html()
    print("csrf token in use: %s" % client.csrf_token)
    print("list response: %d bytes" % len(list_html))
    if args.dump_dir:
        os.makedirs(args.dump_dir, exist_ok=True)
        with open(os.path.join(args.dump_dir, "list.html"), "w", encoding="utf-8") as handle:
            handle.write(list_html)

    items = parse_inbox_list(list_html, args.list_regex)
    print("messages found: %d" % len(items))
    for num, key, row_text in items[:10]:
        print("  num=%s key=%s | %s" % (num, key, " ".join(row_text.split())[:100]))
    if not items:
        print("No messages matched. Either the inbox is empty, the session "
              "expired, or the list pattern needs updating (see --dump-dir).")
        return

    num, key, row_text = items[0]
    view_html = client.fetch_view_html(num, key)
    if args.dump_dir:
        with open(os.path.join(args.dump_dir, "view.html"), "w", encoding="utf-8") as handle:
            handle.write(view_html)
    parsed = parse_message_view(view_html, row_text)
    print("\nnewest message parsed as:")
    for field in ("sender", "recipient", "subject"):
        print("  %-9s %s" % (field + ":", parsed[field]))
    print("  body:     %s" % " ".join(parsed["body_text"].split())[:300])


if __name__ == "__main__":
    main()
