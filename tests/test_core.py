"""Offline tests: python -m pytest tests -q

No network access or real mail account is needed. IMAP is exercised
against a small in-memory fake of the imaplib connection.
"""

import json
import os
import smtplib
import sys
import time
from email.message import EmailMessage

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import ConfigStore, normalize_account, parse_accounts_csv  # noqa: E402
from app.database import Database  # noqa: E402
from app.events import EventHub  # noqa: E402
from app.extractor import extract_link, extract_otp, html_to_text  # noqa: E402
from app.imap_worker import ImapWorker  # noqa: E402
from app.ingest import Ingestor  # noqa: E402
from app.instaddr_poller import parse_inbox_list, parse_message_view  # noqa: E402


def make_mime(subject, body, to="me@gmail.com", sender="Service <no-reply@service.com>",
              html=None, message_id=None):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = "Tue, 10 Nov 2026 10:00:00 +0000"
    msg["Message-ID"] = message_id or "<%s@test>" % abs(hash((subject, body, to)))
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


@pytest.fixture()
def env(tmp_path):
    store = ConfigStore(str(tmp_path / "config.json"))
    db = Database(str(tmp_path / "emails.db"))
    ingestor = Ingestor(db, EventHub(), store)
    yield store, db, ingestor
    db.close()


# ---- extraction -------------------------------------------------------

@pytest.mark.parametrize("subject,body,expected", [
    ("Your verification code", "Use 482913 to finish signing in.", "482913"),
    ("123456 is your login code", "", "123456"),
    ("Sign in", "Your one-time passcode is 123-456. It expires in 10 minutes.", "123456"),
    ("Google", "G-778812 is your Google verification code.", "778812"),
    ("Security alert", "Enter this code: 9041\nCopyright 2026 Example Inc.", "9041"),
    ("Invoice 2026-11-12", "Total due: $1,299.00 for order #88213344.", ""),
    ("Newsletter", "We shipped 2500 units in 2026. Call 555-0100.", ""),
    ("Confirm", "Your code is AB12CD", "AB12CD"),
])
def test_extract_otp(subject, body, expected):
    assert extract_otp(subject, body) == expected


def test_extract_link_prefers_verification():
    html = ('<a href="https://x.com/brand">Twitter</a>'
            '<a href="https://app.example.com/account/verify?token=abcdefghijklmnopqrstuvwxyz123456">Verify email</a>'
            '<a href="https://example.com/unsubscribe?u=1">Unsubscribe</a>')
    assert extract_link("", html).startswith("https://app.example.com/account/verify")
    assert extract_link("Docs: https://example.com/blog/post", "") == ""
    assert extract_link("Reset here: https://example.com/reset/abc.", "") == "https://example.com/reset/abc"


def test_html_to_text_strips_scripts():
    text = html_to_text("<html><head><style>p{}</style></head><body><script>x()</script>"
                        "<p>Hello<br>World</p><p>Code <b>123</b> here</p></body></html>")
    assert text == "Hello\nWorld\n\nCode 123 here"


# ---- config -----------------------------------------------------------

def test_csv_import_and_validation():
    csv_text = ("platform,email,app_password,session_key\n"
                "gmail,A@Gmail.com,abcd efgh ijkl mnop,\n"
                ",b@outlook.com,secret,\n"
                "instaddr,label-1,,deadbeef\n"
                "gmail,broken@gmail.com,,\n")
    accounts, errors = parse_accounts_csv(csv_text)
    assert [a["platform"] for a in accounts] == ["gmail", "outlook", "instaddr"]
    assert accounts[0]["email"] == "a@gmail.com"
    assert accounts[0]["password"] == "abcdefghijklmnop"
    assert accounts[0]["host"] == "imap.gmail.com"
    assert accounts[1]["host"] == "outlook.office365.com"
    assert len(errors) == 1 and "line 5" in errors[0]


def test_config_store_roundtrip(tmp_path):
    path = str(tmp_path / "config.json")
    store = ConfigStore(path)
    assert store.get("webhook_secret")
    store.add_accounts([normalize_account({"email": "a@gmail.com", "password": "p"})])
    again = ConfigStore(path)
    assert [a["email"] for a in again.accounts()] == ["a@gmail.com"]
    assert again.remove_account(again.accounts()[0]["id"])


# ---- ingest and database ---------------------------------------------

def test_ingest_dedupe_and_filters(env):
    _store, db, ingestor = env
    raw = make_mime("Your code", "Your verification code is 111222", message_id="<one@test>")
    first = ingestor.ingest_mime(raw, "gmail", "me@gmail.com")
    assert first is not None
    assert ingestor.ingest_mime(raw, "gmail", "me@gmail.com") is None
    # Same message in a different mailbox is a separate row.
    assert ingestor.ingest_mime(raw, "outlook", "other@outlook.com") is not None

    row = db.get_email(first)
    assert row["source_platform"] == "Gmail"
    assert row["extracted_otp"] == "111222"
    assert row["extracted_otp_or_link"] == "111222"
    assert row["custom_domain"] == "gmail.com"
    assert row["received_at"] == "2026-11-10T10:00:00Z"

    total, rows = db.query_emails(platform="Outlook")
    assert total == 1 and rows[0]["account_email"] == "other@outlook.com"
    assert db.query_emails(query="111222 verification")[0] == 2
    assert db.query_emails(query="100%_nothing")[0] == 0
    assert db.set_read(first, True)
    assert db.query_emails(unread_only=True)[0] == 1
    assert db.facets()["unread"] == 1


def test_forward_domain_relabels_as_instaddr(env):
    store, db, ingestor = env
    store._data["instaddr"]["forward_domains"] = ["mydomain.com"]
    raw = make_mime("Welcome", "Confirm: https://svc.io/confirm/abc", to="shop1@mydomain.com")
    row = db.get_email(ingestor.ingest_mime(raw, "gmail", "master@gmail.com"))
    assert row["source_platform"] == "InstAddr"
    assert row["account_email"] == "shop1@mydomain.com"
    assert row["custom_domain"] == "mydomain.com"
    assert row["extracted_link"] == "https://svc.io/confirm/abc"


# ---- IMAP worker with a fake connection ------------------------------

class FakeImap:
    def __init__(self, messages):
        self.messages = dict(messages)
        self.fetch_calls = []

    def select(self, _mailbox, readonly=False):
        assert readonly is True
        return "OK", [str(len(self.messages)).encode()]

    def response(self, _name):
        return "UIDVALIDITY", [b"77"]

    def uid(self, command, *args):
        if command == "SEARCH":
            uids = sorted(self.messages)
            if args[1] == "UID":
                low = int(args[2].split(":")[0])
                found = [u for u in uids if u >= low] or uids[-1:]
            else:
                found = uids
            return "OK", [" ".join(str(u) for u in found).encode()]
        uid = int(args[0])
        self.fetch_calls.append(uid)
        return "OK", [(b"1 (UID %d BODY[] {10}" % uid, self.messages[uid]), b")"]


def test_imap_sync_incremental(env):
    store, db, ingestor = env
    store._data["imap"]["initial_fetch"] = 2
    account = normalize_account({"email": "me@gmail.com", "password": "p"})
    worker = ImapWorker(account, store, db, ingestor, None, lambda *a: None)
    conn = FakeImap({uid: make_mime("Mail %d" % uid, "body %d" % uid) for uid in (1, 2, 3)})

    assert worker._sync_folder(conn, "INBOX") == 2          # newest two only
    assert conn.fetch_calls == [2, 3]
    assert worker._sync_folder(conn, "INBOX") == 0          # nothing new
    conn.messages[4] = make_mime("Mail 4", "Your OTP is 654321")
    assert worker._sync_folder(conn, "INBOX") == 1
    assert conn.fetch_calls == [2, 3, 4]
    assert db.get_state("imap:%s:INBOX" % account["id"]) == "77:4"


# ---- InstAddr parsing -------------------------------------------------

def test_instaddr_parsers():
    list_html = ("<div onclick=\"openMailData('1001', 'abcdef0123', 'x')\">"
                 "noreply@site.com Verify your account</div>"
                 "<script>openMailData('1001', 'abcdef0123', 'x');"
                 "openMailData('1000', 'ffff0000', 'y');</script>")
    items = parse_inbox_list(list_html)
    assert [(n, k) for n, k, _ in items] == [("1001", "abcdef0123"), ("1000", "ffff0000")]

    view_html = ("<html><body><div class='full'>Verify your account</div>"
                 "<div>From: Site &lt;noreply@site.com&gt;</div>"
                 "<div>To: box7@mydomain.com</div>"
                 "<div dir='ltr'><p>Your code is 445566</p></div></body></html>")
    parsed = parse_message_view(view_html)
    assert parsed["subject"] == "Verify your account"
    assert "noreply@site.com" in parsed["sender"]
    assert parsed["recipient"] == "box7@mydomain.com"
    assert "445566" in parsed["body_text"]


# ---- HTTP API, webhook, WebSocket ------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main
    monkeypatch.setattr(main, "CONFIG_PATH", str(tmp_path / "config.json"))
    with TestClient(main.app) as test_client:
        yield test_client, main.state


def test_api_webhook_and_live_feed(client):
    http, state = client
    secret = state.store.get("webhook_secret")
    assert http.get("/").status_code == 200

    raw = make_mime("Code", "Your security code: 808080", to="x@mydomain.com")
    assert http.post("/api/webhook/inbound?to=x@mydomain.com", content=raw).status_code == 401

    with http.websocket_connect("/ws") as socket:
        res = http.post("/api/webhook/inbound?to=x@mydomain.com&token=" + secret,
                        content=raw, headers={"Content-Type": "message/rfc822"})
        assert res.json()["stored"] is True
        event = socket.receive_json()
        assert event["type"] == "new_email" and event["email"]["extracted_otp"] == "808080"

    res = http.post("/api/webhook/inbound", headers={"X-Webhook-Secret": secret},
                    json={"from": "a@b.com", "to": "y@other.org", "subject": "Hi",
                          "html": "<a href='https://b.com/activate/1'>Activate</a>"})
    assert res.json()["stored"] is True
    res = http.post("/api/webhook/inbound?token=" + secret,
                    data={"recipient": "z@mg.net", "sender": "s@t.com", "subject": "Form",
                          "body-plain": "pin 4321"})
    assert res.json()["stored"] is True

    listing = http.get("/api/emails", params={"platform": "InstAddr"}).json()
    assert listing["total"] == 3
    assert http.get("/api/emails", params={"domain": "other.org"}).json()["total"] == 1
    assert http.get("/api/emails", params={"q": "808080"}).json()["total"] == 1
    mail_id = listing["emails"][0]["id"]
    assert http.post("/api/emails/%d/read" % mail_id, json={"is_read": True}).json()["ok"]
    assert http.get("/api/facets").json()["unread"] == 2
    assert http.post("/api/emails/read-all", json={}).json()["updated"] == 3
    assert http.delete("/api/emails/%d" % mail_id).json()["ok"]
    assert http.get("/api/emails/%d" % mail_id).status_code == 404


def test_accounts_api_hides_secrets(client):
    http, state = client
    csv_text = "platform,email,password,session_key\ninstaddr,label-1,,deadbeef\n"
    res = http.post("/api/accounts/import", files={"file": ("a.csv", csv_text, "text/csv")})
    assert res.json()["added"] == 1
    assert http.post("/api/accounts", json={"platform": "gmail", "email": "x@gmail.com"}).status_code == 400
    body = http.get("/api/accounts").text
    assert "label-1" in body and "deadbeef" not in body
    account_id = json.loads(body)["accounts"][0]["id"]
    assert http.delete("/api/accounts/" + account_id).json()["ok"]
    assert state.store.accounts() == []


def test_dashboard_password(client):
    http, state = client
    state.store._data["server"]["dashboard_password"] = "s3cret"
    assert http.get("/api/facets").status_code == 401
    assert http.get("/api/facets", auth=("any", "wrong")).status_code == 401
    assert http.get("/api/facets", auth=("any", "s3cret")).status_code == 200
    assert http.get("/api/facets").status_code == 200  # session cookie now set


# ---- SMTP catch-all receiver -----------------------------------------

def test_smtp_receiver(env):
    from app.smtp_receiver import SmtpReceiver
    store, db, ingestor = env
    store._data["smtp_receiver"] = {"enabled": True, "host": "127.0.0.1", "port": 12525}
    receiver = SmtpReceiver(ingestor, store)
    assert receiver.start()
    try:
        with smtplib.SMTP("127.0.0.1", 12525, timeout=10) as smtp:
            smtp.sendmail("svc@site.com", ["catch@anydomain.io"],
                          make_mime("Hello", "Your verification code is 246810",
                                    to="catch@anydomain.io"))
        time.sleep(0.2)
    finally:
        receiver.stop()
    total, rows = db.query_emails(account="catch@anydomain.io")
    assert total == 1 and rows[0]["extracted_otp"] == "246810"
    assert rows[0]["source_platform"] == "InstAddr"


# ---- InstAddr Playwright worker against a local fake site ------------

class FakeInstAddrSite:
    """Tiny stand-in for the InstAddr web inbox (list, paging, view)."""

    PAGE_SIZE = 5

    def __init__(self):
        import http.server
        import threading
        site = self
        self.mails = []            # newest first: (num, key, to_address)
        self.fail_view = set()
        self.list_requests = 0
        self.page_loads = 0
        self.token = "sub9"        # rotates like a real CSRF sub-token
        self.rich_rows = False     # rows like "04:06 (5min) | A <a@b> >> to@dom"
        self.frame_bodies = False  # body served from a separate iframe URL

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, body, status=200, cookie=None):
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                from urllib.parse import parse_qs, urlsplit
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                cookies = self.headers.get("Cookie", "")
                if parts.path == "/recv.php":
                    site.page_loads += 1
                    cookie = None if "cookie_sessionhash" in cookies else "cookie_sessionhash=SESSION1; Path=/"
                    self._send("<html><body><script>fetch('/recv._ajax.php?nopost=1"
                               "&csrf_token_check=tok&csrf_subtoken_check=%s')</script>"
                               "</body></html>" % site.token, cookie=cookie)
                elif parts.path == "/body.php":
                    num = query.get("num", [""])[0]
                    self._send("<html><head><style>.header{background:#111}</style></head><body>"
                               "<table class='header'><tr><td class='btn'>Body of %s code 9%s</td>"
                               "</tr></table><img src='/img.php?n=%s' width='10'></body></html>"
                               % (num, num.zfill(5), num))
                elif parts.path == "/img.php":
                    if "cookie_sessionhash" not in cookies:
                        self._send("denied", 403)
                        return
                    data = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"
                    self.send_response(200)
                    self.send_header("Content-Type", "image/gif")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif parts.path == "/recv._ajax.php":
                    site.list_requests += 1
                    if "cookie_sessionhash" not in cookies:
                        self._send("denied", 403)
                        return
                    if query.get("csrf_subtoken_check") != [site.token]:
                        self._send("")          # stale token: silently empty
                        return
                    page = int(query.get("page", ["0"])[0])
                    rows = site.mails[page * site.PAGE_SIZE:(page + 1) * site.PAGE_SIZE]
                    if site.rich_rows:
                        self._send("".join(
                            "<div onclick=\"openMailData('%s', '%s', 'x')\">04:06 (5min) | "
                            "Svc Team &lt;sender@svc.com&gt; &raquo; %s<br>Subject of %s</div>"
                            % (num, key, to, num) for num, key, to in rows))
                        return
                    self._send("".join(
                        "<div onclick=\"openMailData('%s', '%s', 'x')\">sender@svc.com</div>"
                        % (num, key) for num, key, _to in rows))
                else:
                    self._send("not found", 404)

            def do_POST(self):
                from urllib.parse import parse_qs
                length = int(self.headers.get("Content-Length", "0"))
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                num = form.get("num", [""])[0]
                if num in site.fail_view:
                    self._send("temporary failure", 500)
                    return
                to_addr = next((to for n, _k, to in site.mails if n == num), "")
                if site.frame_bodies:
                    self._send("<html><body><div id='area_data'><div>This message has been "
                               "sent from \"bounces+1-%s@svc.com\"</div>"
                               "<iframe src='/body.php?num=%s'></iframe></div></body></html>"
                               % (to_addr.replace("@", "="), num))
                    return
                self._send("<html><body><div class='full'>Mail %s</div>"
                           "<div>From: Svc &lt;sender@svc.com&gt;</div><div>To: %s</div>"
                           "<div dir='ltr'>Your verification code is 1%s</div></body></html>"
                           % (num, to_addr, num.zfill(5)))

            def log_message(self, *_args):
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, count, domain):
        for _ in range(count):
            num = str(1000 + len(self.mails))
            self.mails.insert(0, (num, "k" + num, "box%s@%s" % (num, domain)))

    def close(self):
        self.server.shutdown()


def _wait_for(predicate, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return False


def test_instaddr_browser_worker(env, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    import app.instaddr_browser as browser_module
    from app.instaddr_browser import InstAddrBrowserService

    # Keep retrying the unreadable message for the whole test (no row fallback).
    monkeypatch.setattr(browser_module, "MAX_MESSAGE_ATTEMPTS", 1000)
    store, db, ingestor = env
    site = FakeInstAddrSite()
    site.add(8, "old-domain.com")
    store._data["instaddr"].update({"base_url": site.base_url, "poll_interval": 5,
                                    "initial_fetch": 3})
    store._data["instaddr"]["browser"].update({
        "channel": os.environ.get("MAILAGG_TEST_CHANNEL", ""),
        "page_param": "page", "capture_timeout": 5})
    # Legacy cookie in config seeds the browser session on first start.
    account = normalize_account({"platform": "instaddr", "email": "acct-1",
                                 "session_key": "SESSION1", "mode": "browser"})
    statuses = {}
    service = InstAddrBrowserService(
        store, db, ingestor,
        lambda account_id, state, detail="", new=0: statuses.update({account_id: (state, detail)}))
    service.set_accounts([account])
    try:
        service.start()
        if not _wait_for(lambda: statuses.get(account["id"], ("",))[0] in ("ok", "error")):
            pytest.fail("worker never reported a status")
        state, detail = statuses[account["id"]]
        if state == "error" and "Executable doesn't exist" in detail:
            pytest.skip("no Chromium installed for Playwright")
        assert state == "ok", detail

        # First run imports only the newest 3 of the 8 existing mails.
        assert db.query_emails()[0] == 3
        # Session (storage state) is now persisted in the database, not a file.
        session = db.get_instaddr_session(account["id"])
        assert session is not None and session["storage_state"]

        # Burst of 12 mails across two domains (more than two list pages),
        # with one message failing to open on the first attempts.
        site.add(6, "shop-a.com")
        site.add(6, "shop-b.net")
        flaky, _key, flaky_to = site.mails[4]
        site.fail_view.add(flaky)
        # The 11 readable mails arrive while the unreadable one keeps failing.
        assert _wait_for(lambda: db.query_emails()[0] == 14), db.query_emails()[0]
        assert statuses[account["id"]][0] == "ok"
        site.fail_view.clear()
        assert _wait_for(lambda: db.query_emails()[0] == 15), db.query_emails()[0]

        assert db.query_emails(domain="shop-a.com")[0] == 6
        assert db.query_emails(domain="shop-b.net")[0] == 6
        _total, rows = db.query_emails(query="Mail " + flaky)
        assert rows and rows[0]["source_platform"] == "InstAddr"
        assert rows[0]["extracted_otp"] == "1" + flaky.zfill(5)
        assert rows[0]["account_email"] == flaky_to

        # Steady state: no duplicates on further polls.
        time.sleep(6)
        assert db.query_emails()[0] == 15
    finally:
        service.stop()
        service.join(timeout=15)
        site.close()


# ---- new: From/To parsing, VERP, proxy, link endpoint ----------------

def test_header_arrow_parsing_real_examples():
    from app.instaddr_poller import parse_header_line, verp_recipient
    sender, recipient = parse_header_line(
        "04:06 (22min) | Stake.com <noreply@mail.stake.com> \u00bb andbadend@lamha.pl")
    assert sender == "Stake.com <noreply@mail.stake.com>"
    assert recipient == "andbadend@lamha.pl"
    sender, recipient = parse_header_line(
        "01:35 (2hour) | BC.GAME Support <info@verification.bcgame.ad> \u00bb siradd@lamha.pl")
    assert sender == "BC.GAME Support <info@verification.bcgame.ad>"
    assert recipient == "siradd@lamha.pl"
    # VERP fallback when the arrow line is absent.
    assert verp_recipient('bounces+15421720-b1e0-siradd=lamha.pl@mail.stake.com') == "siradd@lamha.pl"
    _s, r = parse_header_line('Return-Path: <bounces+9-fun=lamha.pl@mail.x.com>')
    assert r == "fun@lamha.pl"


def test_message_view_not_inverted_and_clean_body():
    from app.instaddr_poller import parse_message_view
    view = ('<html><body><div id="toolbar"><button>Delete</button><button>Reply</button></div>'
            '<div class="mail_list">previous mail in list</div>'
            '<div class="full">Verify your email</div>'
            '<div>This message has been sent from "bounces+15421720-b1e0-fun=lamha.pl@mail.stake.com"</div>'
            '<div dir="ltr"><style>.x{color:#fff;background:#000}</style>'
            '<p>C\u00f3digo de verifica\u00e7\u00e3o 778812</p>'
            '<img src="https://track.stake.com/pixel.png"></div></body></html>')
    row = "Stake.com <noreply@mail.stake.com> \u00bb fun@lamha.pl"
    parsed = parse_message_view(view, row)
    # From is the real sender, To is the user's domain address (not inverted).
    assert "noreply@mail.stake.com" in parsed["sender"]
    assert "bounces" not in parsed["sender"]
    assert parsed["recipient"] == "fun@lamha.pl"
    # Body keeps styling + remote image + exact UTF-8, drops m.kuku.lu chrome.
    assert "track.stake.com/pixel.png" in parsed["body_html"]
    assert "color:#fff" in parsed["body_html"]
    assert "Delete" not in parsed["body_html"] and "previous mail in list" not in parsed["body_html"]
    assert "C\u00f3digo de verifica\u00e7\u00e3o 778812" in parsed["body_text"]


def test_parse_proxy():
    from app.instaddr_browser import parse_proxy
    assert parse_proxy("") is None
    assert parse_proxy("http://h:8080") == {"server": "http://h:8080"}
    assert parse_proxy("user:pass@host:3128") == {
        "server": "http://host:3128", "username": "user", "password": "pass"}
    socks = parse_proxy("socks5://1.2.3.4:1080")
    assert socks["server"] == "socks5://1.2.3.4:1080"


def test_link_endpoint_zero_trust(client):
    http, state = client
    token = state.store.get("link_token")
    assert http.post("/api/accounts/link?token=wrong",
                     json={"email": "box@lamha.pl", "session_key": "SHASH1"}).status_code == 401
    res = http.post("/api/accounts/link?token=" + token,
                    json={"email": "box@lamha.pl", "session_key": "SHASH1",
                          "csrf_token": "C1", "proxy": "http://p:8080"})
    assert res.status_code == 200 and res.json()["ok"]
    assert res.json()["account"]["mode"] == "browser"
    assert res.json()["account"]["proxy_set"] is True

    accounts = state.store.accounts()
    linked = [a for a in accounts if a["email"] == "box@lamha.pl"][0]
    assert linked["session_key"] == "SHASH1" and linked["proxy"] == "http://p:8080"
    # The secret session token is never exposed by the accounts API.
    assert "SHASH1" not in http.get("/api/accounts").text
    # The shash alias is accepted too (bookmarklet uses document.cookie names).
    assert http.post("/api/accounts/link?token=" + token,
                     json={"email": "two@lamha.pl", "shash": "SHASH2"}).json()["ok"]


def test_two_accounts_poll_concurrently(env):
    pytest.importorskip("playwright.sync_api")
    from app.instaddr_browser import InstAddrBrowserService

    store, db, ingestor = env
    site_a = FakeInstAddrSite()
    site_b = FakeInstAddrSite()
    site_a.add(4, "a.com")
    site_b.add(4, "b.net")
    store._data["instaddr"].update({"poll_interval": 2, "initial_fetch": 20})
    store._data["instaddr"]["browser"]["channel"] = os.environ.get("MAILAGG_TEST_CHANNEL", "")

    # Each account has its own site (base_url) and its own injected session.
    acc_a = normalize_account({"platform": "instaddr", "email": "acct-a", "mode": "browser",
                               "session_key": "SA", "base_url": site_a.base_url})
    acc_b = normalize_account({"platform": "instaddr", "email": "acct-b", "mode": "browser",
                               "session_key": "SB", "base_url": site_b.base_url})
    statuses = {}
    service = InstAddrBrowserService(
        store, db, ingestor,
        lambda aid, st, detail="", new=0: statuses.update({aid: st}))
    service.set_accounts([acc_a, acc_b])
    try:
        service.start()
        if not _wait_for(lambda: db.query_emails()[0] == 8, timeout=45):
            state_a = statuses.get(acc_a["id"]); state_b = statuses.get(acc_b["id"])
            if "error" in (state_a, state_b):
                pytest.skip("no Chromium available for Playwright")
            pytest.fail("accounts did not both deliver: %s / got %d"
                        % (statuses, db.query_emails()[0]))
        assert db.query_emails(domain="a.com")[0] == 4
        assert db.query_emails(domain="b.net")[0] == 4
        assert statuses[acc_a["id"]] == "ok" and statuses[acc_b["id"]] == "ok"
    finally:
        service.stop()
        service.join(timeout=15)
        site_a.close()
        site_b.close()


# ---- new: config null-safety, link parsing, validation, DB sessions --

def test_config_null_returns_default(tmp_path):
    path = str(tmp_path / "config.json")
    open(path, "w").write('{"instaddr": {"forward_domains": null, '
                          '"browser": {"headless": null}}, "accounts": []}')
    store = ConfigStore(path)
    assert store.get("instaddr", "forward_domains", default=[]) == []
    assert store.get("instaddr", "browser", "headless", default=True) is True
    assert store.get("instaddr", default={}) is not None


def test_parse_link_payload_variants():
    from app.instaddr_link import parse_link_payload, cookies_from_string
    # bare token
    p = parse_link_payload({"email": "Box@Lamha.PL", "session_key": "  abcdef0123456  "})
    assert p["session_hash"] == "abcdef0123456" and p["email"] == "box@lamha.pl"
    # shash alias + csrf + proxy + ua
    p = parse_link_payload({"shash": "S1", "csrf_token": "C1", "proxy": "http://p:8080",
                            "user_agent": "UA/1"})
    assert (p["session_hash"], p["csrf_token"], p["proxy"], p["user_agent"]) == \
        ("S1", "C1", "http://p:8080", "UA/1")
    # whole document.cookie dump
    dump = "theme=dark; cookie_sessionhash=HASHXYZ; cookie_csrf_token=TOK9; x=1"
    p = parse_link_payload({"cookies": dump})
    assert p["session_hash"] == "HASHXYZ" and p["csrf_token"] == "TOK9"
    # a cookie string pasted into the session field itself
    p = parse_link_payload({"session_key": "cookie_sessionhash=HH; shash=HH"})
    assert p["session_hash"] == "HH"
    # quoted value
    assert parse_link_payload({"session_key": '"QUOTED"'})["session_hash"] == "QUOTED"
    # garbage / empty never raises and yields empty hash
    assert parse_link_payload(None)["session_hash"] == ""
    assert parse_link_payload("not a dict")["session_hash"] == ""
    assert cookies_from_string("a=1; b=2")["b"] == "2"


class FakeKukuValidation:
    """Minimal m.kuku.lu for validate_session: authenticated vs logged out."""

    def __init__(self, logged_in=True):
        import http.server
        import threading
        site = self
        self.logged_in = logged_in

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                from urllib.parse import urlsplit
                path = urlsplit(self.path).path
                cookies = self.headers.get("Cookie", "")
                if path == "/recv.php":
                    body, cookie = "<html>inbox</html>", "cookie_csrf_token=CSRF9; Path=/"
                    self._send(body, cookie=cookie)
                elif path == "/recv._ajax.php":
                    if site.logged_in and "cookie_sessionhash" in cookies:
                        self._send("<div onclick=\"openMailData('1','k','x')\">row</div>")
                    else:
                        self._send("<html><body>Please login with your password</body></html>")
                else:
                    self._send("nf", 404)

            def _send(self, body, code=200, cookie=None):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def test_validate_session_live_and_loggedout():
    from app.instaddr_link import validate_session
    site = FakeKukuValidation(logged_in=True)
    try:
        res = validate_session(site.base_url, "a-real-looking-hash-123456")
        assert res["ok"] and res["authenticated"] and res["csrf_token"] == "CSRF9"
    finally:
        site.close()
    site = FakeKukuValidation(logged_in=False)
    try:
        res = validate_session(site.base_url, "x")
        assert res["ok"] and not res["authenticated"]
    finally:
        site.close()
    # Unreachable host: ok False, never raises.
    res = validate_session("http://127.0.0.1:1", "hash", timeout=1)
    assert res["ok"] is False and res["authenticated"] is False


def test_db_instaddr_session_store(tmp_path):
    db = Database(str(tmp_path / "emails.db"))
    try:
        db.upsert_instaddr_session("acc1", email="b@x.pl", session_hash="H1",
                                   csrf_token="C1", proxy="http://p:1")
        row = db.get_instaddr_session("acc1")
        assert row["session_hash"] == "H1" and row["status"] == "linked"
        # Partial runtime update keeps other fields; empty re-link keeps storage.
        db.update_instaddr_runtime("acc1", storage_state='{"cookies":[]}', status="ok",
                                   validated=True)
        db.upsert_instaddr_session("acc1", session_hash="H2")  # rotate token only
        row = db.get_instaddr_session("acc1")
        assert row["session_hash"] == "H2" and row["storage_state"] == '{"cookies":[]}'
        assert row["proxy"] == "http://p:1" and row["validated_at"]
        assert len(db.list_instaddr_sessions()) == 1
        db.delete_instaddr_session("acc1")
        assert db.get_instaddr_session("acc1") is None
    finally:
        db.close()


def test_link_endpoint_end_to_end(client, monkeypatch):
    import app.main as main
    http, state = client
    token = state.store.get("link_token")
    # Point validation at an authenticated fake so no real network is needed.
    site = FakeKukuValidation(logged_in=True)
    state.store._data["instaddr"]["base_url"] = site.base_url
    try:
        # (a) manual JSON form (no proxy -> validation reaches the fake site)
        res = http.post("/api/accounts/link?token=" + token,
                        json={"email": "box1@lamha.pl", "session_key": "HASH-AAAA-1111"})
        body = res.json()
        assert res.status_code == 200 and body["ok"] and body["validated"] is True
        acc_id = body["account"]["id"]
        sess = state.db.get_instaddr_session(acc_id)
        assert sess["session_hash"] == "HASH-AAAA-1111" and sess["status"] == "ok"
        assert sess["validated_at"]

        # (a2) a proxy is stored even when validating through it fails (dead proxy):
        # the session is still saved so the worker can use it, status is not "ok".
        res = http.post("/api/accounts/link?token=" + token,
                        json={"email": "box1@lamha.pl", "session_key": "HASH-AAAA-1111",
                              "proxy": "socks5://127.0.0.1:1"})
        assert res.status_code == 200 and res.json()["ok"]
        sess = state.db.get_instaddr_session(acc_id)
        assert sess["proxy"] == "socks5://127.0.0.1:1" and sess["status"] != "ok"

        # (b) bookmarklet style: text/plain body, document.cookie dump, label in query
        dump = "theme=dark; cookie_sessionhash=HASH-BBBB-2222; cookie_csrf_token=TK"
        res = http.post("/api/accounts/link?token=" + token + "&email=box2@lamha.pl",
                        content=dump, headers={"Content-Type": "text/plain"})
        assert res.json()["ok"] and res.json()["validated"]
        box2 = [a for a in state.store.accounts() if a["email"] == "box2@lamha.pl"][0]
        assert state.db.get_instaddr_session(box2["id"])["session_hash"] == "HASH-BBBB-2222"

        # Secrets never leak through the accounts API.
        listing = http.get("/api/accounts").text
        assert "HASH-AAAA-1111" not in listing and "HASH-BBBB-2222" not in listing
        assert '"session_linked": true' in listing.replace(" ", "").replace('":true', '": true') \
            or '"session_linked":true' in listing

        # Wrong token and missing session are clean 4xx, not 500s.
        assert http.post("/api/accounts/link?token=bad", json={"session_key": "x"}).status_code == 401
        assert http.post("/api/accounts/link?token=" + token,
                         json={"email": "e@x.pl"}).status_code == 400
    finally:
        site.close()


def test_worker_uses_db_only_session(env):
    pytest.importorskip("playwright.sync_api")
    from app.instaddr_browser import InstAddrBrowserService

    store, db, ingestor = env
    site = FakeInstAddrSite()
    site.add(3, "dbonly.pl")
    store._data["instaddr"].update({"base_url": site.base_url, "poll_interval": 2,
                                    "initial_fetch": 20})
    store._data["instaddr"]["browser"]["channel"] = os.environ.get("MAILAGG_TEST_CHANNEL", "")
    # Account has NO session_key in config; the session lives only in the DB.
    account = normalize_account({"platform": "instaddr", "email": "dbonly",
                                 "mode": "browser", "base_url": site.base_url})
    db.upsert_instaddr_session(account["id"], email="dbonly", session_hash="SESSION1")

    statuses = {}
    service = InstAddrBrowserService(
        store, db, ingestor,
        lambda aid, st, detail="", new=0: statuses.update({aid: (st, detail)}))
    service.set_accounts([account])
    try:
        service.start()
        if not _wait_for(lambda: db.query_emails()[0] == 3, timeout=45):
            if statuses.get(account["id"], ("", ""))[0] == "error" and \
                    "Executable" in statuses[account["id"]][1]:
                pytest.skip("no Chromium for Playwright")
            pytest.fail("db-only session worker did not deliver: %s" % statuses)
        assert db.get_instaddr_session(account["id"])["status"] == "ok"
        assert db.get_instaddr_session(account["id"])["storage_state"]
    finally:
        service.stop()
        service.join(timeout=15)
        site.close()


# ---- regression: email markup inside the body wrapper is preserved ----

RICH_EMAIL = (
    '<table class="header" width="600"><tr><td id="banner">'
    '<img src="https://cdn.x.com/logo.png" width="120" height="40"></td></tr></table>'
    '<table class="content" style="background:#101010;color:#fff"><tr>'
    '<td class="menu"><a class="btn share" href="https://x.com/verify?token=abc" '
    'style="padding:12px 24px;background:#0a0">Verify</a></td></tr></table>'
    '<form action="https://x.com/s"><button class="button">Go</button></form>'
    '<div class="footer" role="navigation">Footer text <div dir="ltr">inner ltr</div></div>')


@pytest.mark.parametrize("wrapper", [
    '<div id="area_data">%s</div>',
    '<div dir="ltr">%s</div>',
])
def test_body_wrapper_content_is_not_stripped(wrapper):
    from app.instaddr_poller import parse_message_view
    view = ('<html><body><div id="toolbar" class="header"><button>Delete</button></div>'
            '<div class="mail_list">other row</div>' + (wrapper % RICH_EMAIL) +
            '<div class="footer">site footer</div></body></html>')
    html = parse_message_view(view, "A <a@b.com> \u00bb me@lamha.pl")["body_html"]
    for needle in ('class="header"', 'id="banner"', 'logo.png', 'class="menu"',
                   'class="btn share"', 'padding:12px 24px', '<form', 'class="button"',
                   'class="footer"', 'Footer text', 'inner ltr', 'background:#101010'):
        assert needle in html, needle
    # The site chrome outside the wrapper is gone.
    assert "Delete" not in html and "other row" not in html and "site footer" not in html


def test_body_iframe_srcdoc_is_verbatim_and_fallback_is_safe():
    from bs4 import BeautifulSoup
    from app.instaddr_poller import extract_body_container
    raw = '<html><head><style>.header{color:red}</style></head><body><div class="header btn">Hi</div></body></html>'
    soup = BeautifulSoup('<body><iframe srcdoc="%s"></iframe></body>' % raw.replace('"', "&quot;"),
                         "html.parser")
    assert extract_body_container(soup) == raw
    # No wrapper at all: chrome stripping runs and must survive nested junk
    # (decompose() clears child attrs; this used to raise AttributeError).
    page = ('<body><div class="toolbar"><div class="menu"><span class="btn">x</span></div></div>'
            '<p>Mail text 123</p></body>')
    html = extract_body_container(BeautifulSoup(page, "html.parser"))
    assert "Mail text 123" in html and "toolbar" not in html


def test_validation_403_is_inconclusive_and_token_decoded():
    import http.server
    import threading
    from app.instaddr_link import parse_link_payload, validate_session

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        res = validate_session("http://127.0.0.1:%d" % server.server_address[1], "SHASH:abcdef123456")
    finally:
        server.shutdown()
    assert res["ok"] and res["inconclusive"] and not res["authenticated"]
    assert parse_link_payload({"session_key": "SHASH%3Aabc123"})["session_hash"] == "SHASH:abc123"
    assert parse_link_payload({"cookies": "cookie_sessionhash=SHASH%3Aabc123"})["session_hash"] == "SHASH:abc123"


def test_launch_candidates(monkeypatch):
    import app.instaddr_browser as ib
    monkeypatch.setattr(ib.sys, "platform", "win32")
    assert ib.launch_candidates("") == ["msedge", "chrome", ""]
    assert ib.launch_candidates("chrome") == ["chrome", "msedge", ""]
    monkeypatch.setattr(ib.sys, "platform", "linux")
    assert ib.launch_candidates("")[0] == ""
    assert ib.launch_candidates("msedge")[0] == "msedge"


# ---- hard rules from field debugging ----------------------------------

def test_rule1_config_defaults_to_edge_on_windows(tmp_path, monkeypatch):
    import importlib
    import app.config as config
    monkeypatch.setattr(config.sys, "platform", "win32")
    reloaded = importlib.reload(config)
    try:
        store = reloaded.ConfigStore(str(tmp_path / "config.json"))
        assert store.get("instaddr", "browser", "channel") == "msedge"
        saved = json.load(open(str(tmp_path / "config.json")))
        assert saved["instaddr"]["browser"]["channel"] == "msedge"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_rule2_session_token_sanitized_everywhere():
    account = normalize_account({"platform": "instaddr", "email": "x",
                                 "session_key": ' "SHASH%3Aabc123" '})
    assert account["session_key"] == "SHASH:abc123"


def test_rule4_nested_decompose_is_safe():
    from bs4 import BeautifulSoup
    from app.instaddr_poller import extract_body_container
    nested = ('<head><title>t</title><style>s{}</style><noscript><script>a()</script>'
              '<style>x{}</style></noscript></head><body><p>Keep 42</p></body>')
    assert html_to_text(nested) == "Keep 42"
    wrapped = BeautifulSoup('<div id="area_data"><noscript><script>a()</script><b>n</b></noscript>'
                            '<p class="header">Keep</p></div>', "html.parser")
    html = extract_body_container(wrapped)
    assert "script" not in html and 'class="header"' in html


def test_rule5_worker_names_and_reimport_tool(tmp_path):
    import subprocess
    from app.imap_worker import IMAPWorker, ImapWorker
    from app.instaddr_poller import InstAddrWorker
    assert IMAPWorker is ImapWorker and InstAddrWorker is not None

    store = ConfigStore(str(tmp_path / "config.json"))
    db = Database(str(tmp_path / "emails.db"))
    ingestor = Ingestor(db, EventHub(), store)
    ingestor.ingest_fields("instaddr", "a@lamha.pl", "s@x.com", "old", body_html="<p>old</p>",
                           dedupe_key="instaddr-num:1")
    ingestor.ingest_mime(make_mime("Keep me", "gmail body"), "gmail", "me@gmail.com")
    db.instaddr_mark_seen("acc1", ["1"])
    db.set_state("instaddr:acc1:initialized", "1")
    db.upsert_instaddr_session("acc1", email="a", session_hash="SHASH:keep")
    db.close()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run([sys.executable, os.path.join(root, "tools", "instaddr_reimport.py"),
                             "--config", str(tmp_path / "config.json"), "--yes"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    db = Database(str(tmp_path / "emails.db"))
    try:
        assert db.query_emails(platform="InstAddr")[0] == 0
        assert db.query_emails(platform="Gmail")[0] == 1          # other mail untouched
        assert db.instaddr_unseen("acc1", ["1"]) == {"1"}          # will be fetched again
        assert db.get_state("instaddr:acc1:initialized", "") == ""
        assert db.get_instaddr_session("acc1")["session_hash"] == "SHASH:keep"  # link kept
    finally:
        db.close()


# ---- live-sync regressions: stale tokens, poison mail, frames, metadata ----

def _start_service(store, db, ingestor, site, account, **instaddr):
    from app.instaddr_browser import InstAddrBrowserService
    store._data["instaddr"].update({"base_url": site.base_url, "poll_interval": 2,
                                    "initial_fetch": 20})
    store._data["instaddr"].update(instaddr)
    store._data["instaddr"]["browser"]["channel"] = os.environ.get("MAILAGG_TEST_CHANNEL", "")
    statuses = {}
    service = InstAddrBrowserService(
        store, db, ingestor,
        lambda aid, st, detail="", new=0: statuses.update({aid: (st, detail)}))
    service.set_accounts([account])
    service.start()
    return service, statuses


def _skip_without_browser(statuses, account):
    state = statuses.get(account["id"], ("", ""))
    if state[0] == "error" and ("no usable browser" in state[1] or "Executable" in state[1]):
        pytest.skip("no browser available for Playwright")


def test_stale_list_token_recovers_without_waiting_for_heartbeat(env):
    pytest.importorskip("playwright.sync_api")
    store, db, ingestor = env
    site = FakeInstAddrSite()
    site.add(2, "first.pl")
    account = normalize_account({"platform": "instaddr", "email": "stale", "mode": "browser",
                                 "session_key": "SESSION1"})
    service, statuses = _start_service(store, db, ingestor, site, account)
    try:
        if not _wait_for(lambda: db.query_emails()[0] == 2, timeout=45):
            _skip_without_browser(statuses, account)
            pytest.fail("initial import failed: %s" % statuses)
        loads = site.page_loads
        # The site rotates its token: the captured list URL now returns an
        # empty page (HTTP 200). New mail must still arrive promptly.
        site.token = "sub10"
        site.add(1, "after-rotation.pl")
        assert _wait_for(lambda: db.query_emails(domain="after-rotation.pl")[0] == 1, timeout=60), \
            "mail after token rotation never arrived"
        assert site.page_loads > loads          # recovered by reloading the tab
    finally:
        service.stop()
        service.join(timeout=15)
        site.close()


def test_unreadable_message_is_stored_from_its_row(env):
    pytest.importorskip("playwright.sync_api")
    store, db, ingestor = env
    site = FakeInstAddrSite()
    site.rich_rows = True
    site.add(3, "poison.pl")
    bad = site.mails[0][0]                      # the newest one never opens
    site.fail_view.add(bad)
    account = normalize_account({"platform": "instaddr", "email": "poison", "mode": "browser",
                                 "session_key": "SESSION1"})
    service, statuses = _start_service(store, db, ingestor, site, account)
    try:
        if not _wait_for(lambda: db.query_emails()[0] >= 2, timeout=45):
            _skip_without_browser(statuses, account)
            pytest.fail("readable mail was blocked: %s" % statuses)
        assert _wait_for(lambda: db.query_emails()[0] == 3, timeout=45)
        _total, rows = db.query_emails(query="Subject of " + bad)
        row = db.get_email(rows[0]["id"])
        assert row["recipient"] == "box%s@poison.pl" % bad
        assert "sender@svc.com" in row["sender"] and "could not be read" in row["body_text"]
    finally:
        service.stop()
        service.join(timeout=15)
        site.close()


def test_frame_body_images_headers_and_timestamp(env):
    pytest.importorskip("playwright.sync_api")
    from datetime import datetime, timezone
    store, db, ingestor = env
    site = FakeInstAddrSite()
    site.rich_rows = True
    site.frame_bodies = True
    site.add(1, "lamha.pl")
    num, _key, to_addr = site.mails[0]
    account = normalize_account({"platform": "instaddr", "email": "frames", "mode": "browser",
                                 "session_key": "SESSION1"})
    service, statuses = _start_service(store, db, ingestor, site, account)
    try:
        if not _wait_for(lambda: db.query_emails()[0] == 1, timeout=45):
            _skip_without_browser(statuses, account)
            pytest.fail("message not stored: %s" % statuses)
        row = db.get_email(db.query_emails()[1][0]["id"])
        # Body came from the separate frame document, markup intact.
        assert "<table class='header'>" in row["body_html"] or 'class="header"' in row["body_html"]
        assert "Body of %s" % num in row["body_text"]
        assert row["extracted_otp"] == "9" + num.zfill(5)
        # The session-only image was embedded so it renders in the dashboard.
        assert "data:image/gif;base64," in row["body_html"] and "/img.php" not in row["body_html"]
        # Metadata: real sender, real recipient, subject from the row.
        assert row["sender"] == "Svc Team <sender@svc.com>"
        assert row["recipient"] == to_addr and row["subject"] == "Subject of " + num
        # Timestamp from the row's age (5 minutes ago), not the fetch time.
        received = datetime.strptime(row["received_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - received).total_seconds()
        assert 4 * 60 <= age <= 7 * 60, age
        # Full header block is kept.
        for needle in ("From: Svc Team <sender@svc.com>", "To: " + to_addr,
                       "Return-Path: <bounces+1-", "X-InstAddr-Message-Number: " + num):
            assert needle in row["raw_headers"], needle
    finally:
        service.stop()
        service.join(timeout=15)
        site.close()


def test_wrapper_selection_and_helpers():
    from datetime import datetime, timezone
    from app.instaddr_poller import estimate_received_at, parse_message_view, row_subject
    # #area_data holds only the header; the body sits in a separate dir=ltr block.
    view = ('<body><div id="area_data"><div class="full">Subj</div></div>'
            '<div dir="ltr"><table class="footer"><tr><td>Real body 445566</td></tr></table>'
            '<img data-src="https://cdn.x/a.png"></div></body>')
    parsed = parse_message_view(view, "")
    assert "Real body 445566" in parsed["body_html"] and 'class="footer"' in parsed["body_html"]
    assert 'src="https://cdn.x/a.png"' in parsed["body_html"]
    # #area_data holds the whole mail; an inner dir=ltr is only a fragment of it.
    view = ('<body><div id="area_data"><table><tr><td>' + "Long intro text. " * 20 +
            '</td></tr></table><div dir="ltr">tiny</div><p>Footer part</p></div></body>')
    parsed = parse_message_view(view, "")
    assert "Long intro text." in parsed["body_html"] and "Footer part" in parsed["body_html"]

    now = datetime(2026, 11, 13, 19, 28, 40, tzinfo=timezone.utc)
    assert estimate_received_at("04:06 (22min) | x", "", now) == "2026-11-13T19:06:00Z"
    assert estimate_received_at("01:35 (2hour) | x", "", now) == "2026-11-13T16:35:00Z"
    assert estimate_received_at("no age here", "", now) == "2026-11-13T19:28:40Z"
    assert row_subject("04:06 (22min) | A <a@b.com> \u00bb c@d.pl\nYour code is 1") == "Your code is 1"


def test_raw_headers_kept_for_imap_mail_and_old_db_migrates(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE emails (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "source_platform TEXT NOT NULL, account_email TEXT NOT NULL, "
                 "custom_domain TEXT NOT NULL DEFAULT '', recipient TEXT NOT NULL DEFAULT '', "
                 "sender TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '', "
                 "body_text TEXT NOT NULL DEFAULT '', body_html TEXT NOT NULL DEFAULT '', "
                 "extracted_otp TEXT NOT NULL DEFAULT '', extracted_link TEXT NOT NULL DEFAULT '', "
                 "extracted_otp_or_link TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL DEFAULT '', "
                 "dedupe_hash TEXT NOT NULL UNIQUE, received_at TEXT NOT NULL, "
                 "fetched_at TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0)")
    conn.commit()
    conn.close()
    db = Database(path)                          # must add the new column
    store = ConfigStore(str(tmp_path / "config.json"))
    try:
        ingestor = Ingestor(db, EventHub(), store)
        row = db.get_email(ingestor.ingest_mime(
            make_mime("Hi", "body", message_id="<hdr@test>"), "gmail", "me@gmail.com"))
        assert "Message-ID: <hdr@test>" in row["raw_headers"]
        assert "From: Service <no-reply@service.com>" in row["raw_headers"]
        assert "body" not in row["raw_headers"]
    finally:
        db.close()
