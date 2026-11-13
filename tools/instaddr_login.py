"""Optional/legacy helper: save or check an InstAddr browser login locally.

The normal way to connect an InstAddr account is now zero-trust linking
from the dashboard (Accounts -> Link InstAddr: bookmarklet or token paste),
which needs no command line and no credentials typed into this tool.

This script stays for local/dev use:

  Save a login by hand (opens a visible browser; log in, press Enter):
      python tools/instaddr_login.py --account my-label

  Check a saved login headlessly and dump raw pages for parser tuning:
      python tools/instaddr_login.py --account my-label --check-only --dump-dir debug_out

--account is the label used as "email" for the account in config.json;
a new browser-mode account is created if it does not exist yet.
"""

import argparse
import os
import sys
import time
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import ConfigStore, normalize_account  # noqa: E402
from app.instaddr_browser import (browser_settings, fingerprint,  # noqa: E402
                                  launch_candidates, profile_paths, write_meta)
from app.instaddr_poller import parse_header_line, parse_inbox_list, parse_message_view  # noqa: E402

LIST_ENDPOINT = "recv._ajax.php"


def find_or_add_account(store, label):
    label = label.strip().lower()
    for account in store.accounts():
        if account["platform"] == "instaddr" and label in (account["email"], account["id"]):
            return account
    account = normalize_account({"platform": "instaddr", "email": label, "mode": "browser"})
    store.add_accounts([account])
    print("Added InstAddr account '%s' to %s (restart the app to poll it)." % (label, store.path))
    return account


def discover_list_url(context, settings):
    captured = []
    page = context.new_page()
    try:
        page.on("response", lambda r: captured.append(r.url) if LIST_ENDPOINT in r.url else None)
        page.goto(settings["base_url"] + settings.get("inbox_path", "/recv.php"),
                  wait_until="domcontentloaded", timeout=45000)
        deadline = time.time() + float(settings.get("capture_timeout", 10))
        while not captured and time.time() < deadline:
            page.wait_for_timeout(200)
        html = page.content()
    finally:
        page.close()
    if captured:
        return captured[0]
    cookies = {c["name"]: c["value"] for c in context.cookies(settings["base_url"])}
    csrf = cookies.get("cookie_csrf_token", "")
    if not csrf:
        raise RuntimeError("inbox page set no CSRF cookie; check instaddr.browser.inbox_path")
    return "%s/%s?%s" % (settings["base_url"], LIST_ENDPOINT,
                         urlencode({"nopost": "1", "csrf_token_check": csrf}))


def run_check(context, settings, dump_dir):
    list_url = discover_list_url(context, settings)
    list_html = context.request.get(list_url).text()
    items = parse_inbox_list(list_html, settings["list_regex"])
    print("messages on first list page: %d" % len(items))
    for num, _key, row_text in items[:10]:
        sender, recipient = parse_header_line(row_text)
        print("  num=%s  from=%s  to=%s" % (num, sender[:40], recipient))
    if dump_dir:
        os.makedirs(dump_dir, exist_ok=True)
        open(os.path.join(dump_dir, "list.html"), "w", encoding="utf-8").write(list_html)
    if not items:
        print("No messages matched. Save pages with --dump-dir and adjust "
              "instaddr.list_regex if the inbox is not empty.")
        return
    num, key, row_text = items[0]
    view_html = context.request.post(settings["base_url"] + "/smphone.app.recv.view.php",
                                     form={"num": num, "key": key, "noscroll": "1"}).text()
    if dump_dir:
        open(os.path.join(dump_dir, "view.html"), "w", encoding="utf-8").write(view_html)
    parsed = parse_message_view(view_html, row_text, settings.get("body_selector", ""))
    print("newest message parsed as:")
    for field in ("sender", "recipient", "subject"):
        print("  %-10s %s" % (field + ":", parsed[field]))
    print("  body:      %s" % " ".join(parsed["body_text"].split())[:300])


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True)
    parser.add_argument("--config", default=os.environ.get("MAILAGG_CONFIG", "config.json"))
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--channel", default=None, help="use an installed browser: msedge or chrome")
    parser.add_argument("--dump-dir", default="")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright is not installed. Run:\n"
                 "  pip install playwright\n  python -m playwright install chromium")

    store = ConfigStore(args.config)
    account = find_or_add_account(store, args.account)
    settings = browser_settings(store)
    if args.channel is not None:
        settings["channel"] = args.channel
    state_path, meta_path = profile_paths(store, account["id"])
    if args.check_only and not os.path.exists(state_path) and not account.get("session_key"):
        sys.exit("No saved login or token for this account. Link it from the dashboard first.")

    with sync_playwright() as playwright:
        browser = None
        for channel in launch_candidates(settings.get("channel", "")):
            launch = {"headless": args.check_only}
            if channel:
                launch["channel"] = channel
            try:
                browser = playwright.chromium.launch(**launch)
                break
            except Exception as exc:
                print("could not launch %s: %s" % (channel or "bundled chromium",
                                                   (str(exc).splitlines() or [""])[0][:160]))
        if browser is None:
            sys.exit("No usable browser found (Edge, Chrome or Playwright Chromium).")
        ctx_opts = {"locale": "en-US"}
        if settings.get("user_agent"):
            ctx_opts["user_agent"] = settings["user_agent"]
        if os.path.exists(state_path):
            ctx_opts["storage_state"] = state_path
        context = browser.new_context(**ctx_opts)
        try:
            if not args.check_only:
                page = context.new_page()
                page.goto(settings["base_url"] + "/", wait_until="domcontentloaded")
                print("\nA browser window is open on %s" % settings["base_url"])
                print("1. Log in to InstAddr account '%s' there." % account["email"])
                print("2. Make sure its inbox is visible.")
                input("3. Press Enter here to save the login... ")
                cookies = {c["name"]: c["value"] for c in context.cookies(settings["base_url"])}
                session_hash = cookies.get("cookie_sessionhash") or cookies.get("shash") or ""
                if not session_hash:
                    sys.exit("No session cookie was set; nothing saved.")
                tmp = state_path + ".tmp"
                context.storage_state(path=tmp)
                os.replace(tmp, state_path)
                write_meta(meta_path, session_hash)
                page.close()
                print("Saved login to %s (fingerprint %s)\n" % (state_path, fingerprint(session_hash)))
            run_check(context, settings, args.dump_dir)
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
