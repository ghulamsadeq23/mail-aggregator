"""InstAddr (m.kuku.lu) real-time worker: async Playwright, one task/account.

Design goals (why this module exists):

  * Real time. Every account runs in its own asyncio task, so accounts
    never wait in a queue behind one another. Each task hits the site's
    own lightweight list endpoint (recv._ajax.php) every poll_interval
    seconds (default 2) and pushes new mail to the dashboard over the
    existing WebSocket within a second or two of arrival.
  * Zero-trust linking. A context is driven by an injected session
    (cookie_sessionhash / shash from the bookmarklet or a pasted token)
    or by a saved storage_state. Nobody types credentials into this tool.
    Reloading the inbox page keeps the site renewing its own cookies, and
    the refreshed state is written back to disk so the session survives
    the user logging out or shutting the PC down.
  * Isolation / anti-ban. Each account gets its own browser context with
    its own optional proxy and User-Agent, so accounts never share an IP
    or a cookie jar.

One Chromium process is shared; the per-account contexts are independent.
All Playwright calls run on this module's own event loop thread.
"""

import asyncio
import base64
import collections
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .extractor import html_to_text
from .instaddr_poller import (body_has_content, build_headers, estimate_received_at,
                              parse_header_line, parse_inbox_list, parse_message_view,
                              row_subject)

log = logging.getLogger("instaddr")

LIST_ENDPOINT = "recv._ajax.php"
VIEW_ENDPOINT = "/smphone.app.recv.view.php"
SUBTOKEN_RE = re.compile(r"csrf_subtoken_check=([0-9A-Za-z]+)")
# Cookie names that carry the InstAddr session, in priority order.
SESSION_COOKIE_NAMES = ("cookie_sessionhash", "shash")
MAX_BACKOFF = 60
GAP_WARNING_MIN_ROWS = 5
MAX_MESSAGE_ATTEMPTS = 4
MAX_INLINE_IMAGES = 15
MAX_INLINE_IMAGE_BYTES = 2 * 1024 * 1024


class LoginRequired(Exception):
    """The session is missing or no longer belongs to the account."""


# ---- pure helpers (shared with tools) ---------------------------------

def browser_settings(store):
    settings = store.get("instaddr", default={})
    browser = dict(settings.get("browser") or {})
    browser["base_url"] = str(settings.get("base_url", "https://m.kuku.lu")).rstrip("/")
    browser["user_agent"] = settings.get("user_agent", "")
    browser["list_regex"] = settings.get("list_regex", "")
    browser["body_selector"] = settings.get("body_selector", "")
    browser["poll_interval"] = max(1, int(settings.get("poll_interval", 2)))
    return browser


def profile_paths(store, account_id):
    profile_dir = store.get("instaddr", "browser", "profile_dir", default="browser_profiles")
    if not os.path.isabs(profile_dir):
        profile_dir = os.path.join(os.path.dirname(store.path), profile_dir)
    os.makedirs(profile_dir, exist_ok=True)
    return (os.path.join(profile_dir, account_id + ".json"),
            os.path.join(profile_dir, account_id + ".meta.json"))


def fingerprint(session_hash):
    return hashlib.sha256((session_hash or "").encode("utf-8")).hexdigest()[:16]


def read_meta(meta_path):
    try:
        with open(meta_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def write_meta(meta_path, session_hash):
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump({"session_fp": fingerprint(session_hash),
                   "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, handle)


def with_query(url, **changes):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in changes.items()})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def launch_candidates(configured=""):
    """Browser channels to try, in order. '' means Playwright's bundled build.

    On Windows the bundled chrome-headless-shell.exe is commonly blocked by
    Smart App Control ("spawn UNKNOWN"), so the system Edge is tried first
    there. A channel set in config always goes first; the rest are fallbacks.
    """
    defaults = ["msedge", "chrome", ""] if sys.platform == "win32" else ["", "chrome", "msedge"]
    order = []
    for channel in [str(configured or "").strip()] + defaults if configured else defaults:
        if channel not in order:
            order.append(channel)
    return order


def parse_proxy(proxy_url):
    """Turn 'http://user:pass@host:port' into Playwright's proxy dict."""
    if not proxy_url:
        return None
    parts = urlsplit(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    if not parts.hostname:
        raise ValueError("invalid proxy URL: %s" % proxy_url)
    server = "%s://%s" % (parts.scheme or "http", parts.hostname)
    if parts.port:
        server += ":%d" % parts.port
    proxy = {"server": server}
    if parts.username:
        proxy["username"] = parts.username
    if parts.password:
        proxy["password"] = parts.password
    return proxy


def _session_from_cookies(cookies):
    for name in SESSION_COOKIE_NAMES:
        if cookies.get(name):
            return cookies[name]
    return ""


# ---- the service -------------------------------------------------------

class InstAddrBrowserService(threading.Thread):
    def __init__(self, store, db, ingestor, status_cb):
        super().__init__(name="instaddr-async", daemon=True)
        self.store = store
        self.db = db
        self.ingestor = ingestor
        self.status_cb = status_cb
        self._loop = None
        self._ready = threading.Event()
        self._stopping = False
        self._pending_accounts = []
        self._tasks = {}            # account_id -> asyncio.Task
        self._accounts = {}         # account_id -> account dict
        self._browser = None

    # ---- cross-thread API ---------------------------------------------

    def set_accounts(self, accounts):
        if self._loop is None or not self._loop.is_running():
            self._pending_accounts = list(accounts)
            return
        self._loop.call_soon_threadsafe(self._apply_accounts, list(accounts))

    def stop(self):
        self._stopping = True
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._shutdown)

    # ---- event loop thread --------------------------------------------

    def run(self):
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            self._idle_with_error("Playwright is not installed. Run: pip install playwright "
                                  "&& python -m playwright install chromium")
            return
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as exc:
            log.error("InstAddr async service crashed: %s", exc)
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()

    def _idle_with_error(self, message):
        log.error(message)
        while not self._stopping:
            for account in self._pending_accounts:
                self.status_cb(account["id"], "error", message)
            time.sleep(30)

    async def _main(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            headless = self.store.get("instaddr", "browser", "headless", default=True)
            configured = self.store.get("instaddr", "browser", "channel", default="")
            last_error = None
            self._browser = None
            for channel in launch_candidates(configured):
                options = {"headless": headless}
                if channel:
                    options["channel"] = channel
                try:
                    self._browser = await playwright.chromium.launch(**options)
                    log.info("browser channel in use: %s", channel or "bundled chromium")
                    break
                except Exception as exc:
                    last_error = exc
                    log.warning("could not launch %s: %s", channel or "bundled chromium",
                                (str(exc).splitlines() or [""])[0][:200])
            if self._browser is None:
                raise RuntimeError(
                    "no usable browser. Install Edge/Chrome or run "
                    "'python -m playwright install chromium'. Last error: %s"
                    % (str(last_error).splitlines() or [""])[0][:200])
            log.info("InstAddr async worker started (%s)", self._browser.version)
            if self._pending_accounts:
                self._apply_accounts(self._pending_accounts)
                self._pending_accounts = []
            self._ready.set()
            try:
                while not self._stopping:
                    await asyncio.sleep(0.3)
                    if not self._browser.is_connected():
                        raise RuntimeError("browser process exited")
            finally:
                for task in list(self._tasks.values()):
                    task.cancel()
                await asyncio.gather(*self._tasks.values(), return_exceptions=True)
                try:
                    await self._browser.close()
                except Exception:
                    pass

    def _shutdown(self):
        self._stopping = True

    # ---- account task management (on the loop) ------------------------

    def _apply_accounts(self, accounts):
        wanted = {account["id"]: account for account in accounts}
        for account_id in list(self._tasks):
            if account_id not in wanted or wanted[account_id] != self._accounts.get(account_id):
                self._tasks.pop(account_id).cancel()
                self._accounts.pop(account_id, None)
        for account_id, account in wanted.items():
            if account_id not in self._tasks:
                self._accounts[account_id] = account
                self.status_cb(account_id, "connecting", "")
                self._tasks[account_id] = self._loop.create_task(self._account_loop(account))

    async def _account_loop(self, account):
        """Independent live loop for one account; never blocks the others.

        A real inbox tab stays open per account, exactly like the native web
        inbox. Three things feed the loop:
          * passive: every list response the site's own page requests is
            captured, together with its always-fresh tokens;
          * active: the list endpoint is also fetched every poll_interval;
          * heartbeat: the tab is reloaded every reload_minutes, and at once
            when the list goes stale, renewing cookies and CSRF tokens.
        Every cycle is wrapped, so one bad response becomes a status line and
        a logged traceback, never a dead task.
        """
        live = _Live()
        backoff = 5
        expired_logged = False
        account_id = account["id"]
        try:
            while not self._stopping:
                settings = browser_settings(self.store)
                if account.get("base_url"):
                    settings["base_url"] = account["base_url"].rstrip("/")
                try:
                    session = self._load_session(account)
                    if session is None:
                        self._set_db_status(account_id, "unlinked",
                                            "not linked yet; use Accounts -> Link InstAddr")
                        self.status_cb(account_id, "error", "not linked yet (use Link InstAddr)")
                        await asyncio.sleep(15)
                        continue

                    # A re-link (new token in the DB) rebuilds the context.
                    if live.context is not None and session["session_hash"] != live.opened_hash:
                        await _safe_close(live.context)
                        live = _Live()
                    if live.context is None:
                        live.context = await self._open_context(account, settings, session)
                        live.opened_hash = session["session_hash"]
                        expired_logged = False

                    reload_seconds = max(0.5, float(settings.get("reload_minutes", 5))) * 60
                    if (live.page is None or live.page.is_closed() or not live.list_url
                            or live.force_reload
                            or time.time() - live.last_reload > reload_seconds):
                        await self._reload_page(live, account, settings, session)

                    stored = await self._sync(live, account, settings)
                    self._set_db_status(account_id, "ok", "", validated=True)
                    self.status_cb(account_id, "ok", "", stored)
                    backoff = 5
                    delay = settings.get("poll_interval", 2)
                except asyncio.CancelledError:
                    raise
                except LoginRequired as exc:
                    # Expected: the session is no longer valid. Warn once and
                    # keep the task alive so a re-link recovers by itself.
                    if not expired_logged:
                        log.warning("%s: %s", account["email"], exc)
                        expired_logged = True
                    self._set_db_status(account_id, "expired", str(exc)[:300])
                    self.status_cb(account_id, "error", str(exc)[:300])
                    if live.context is not None:
                        await _safe_close(live.context)
                    opened = live.opened_hash
                    live = _Live()
                    live.opened_hash = opened
                    delay = MAX_BACKOFF
                except Exception as exc:
                    # Unexpected: full traceback to the log, clean line to the UI.
                    log.exception("%s: unexpected error in poll cycle", account["email"])
                    message = "%s: %s" % (exc.__class__.__name__,
                                          (str(exc).splitlines() or [""])[0])
                    self._set_db_status(account_id, "error", message[:300])
                    self.status_cb(account_id, "error", message[:300])
                    live.force_reload = True
                    delay = backoff
                    backoff = min(backoff * 2, MAX_BACKOFF)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass
        finally:
            if live.context is not None:
                await _safe_close(live.context)
            self.status_cb(account_id, "stopped", "")

    # ---- one account's browser work -----------------------------------

    def _set_db_status(self, account_id, status, detail, validated=False):
        try:
            if self.db.get_instaddr_session(account_id) is not None:
                self.db.update_instaddr_runtime(account_id, status=status, detail=detail,
                                                validated=validated)
        except Exception:
            log.debug("could not write session status for %s", account_id, exc_info=True)

    def _load_session(self, account):
        """Return the live session for this account, or None if unlinked.

        The database is the source of truth. A legacy config account that
        still carries a session_key is migrated into the database once.
        """
        session = self.db.get_instaddr_session(account["id"])
        if session is None and account.get("session_key"):
            session = self.db.upsert_instaddr_session(
                account["id"], email=account.get("email", ""),
                session_hash=account["session_key"],
                csrf_token=account.get("csrf_token", ""),
                user_agent=account.get("user_agent", ""),
                proxy=account.get("proxy", ""))
        if session is None or not (session.get("session_hash") or session.get("storage_state")):
            return None
        return session

    async def _open_context(self, account, settings, session):
        options = {"locale": "en-US"}
        user_agent = session.get("user_agent") or account.get("user_agent") or settings.get("user_agent")
        if user_agent:
            options["user_agent"] = user_agent
        proxy = parse_proxy(session.get("proxy") or account.get("proxy", ""))
        if proxy:
            options["proxy"] = proxy

        storage_state = _load_storage_state(session.get("storage_state"))
        if storage_state:
            options["storage_state"] = storage_state
        context = await self._browser.new_context(**options)
        context.set_default_timeout(30000)

        if not storage_state:
            host = settings["base_url"].split("//", 1)[-1].split("/", 1)[0].split(":")[0]
            cookies = [{"name": name, "value": session["session_hash"],
                        "domain": host, "path": "/"} for name in SESSION_COOKIE_NAMES]
            if session.get("csrf_token"):
                cookies.append({"name": "cookie_csrf_token", "value": session["csrf_token"],
                                "domain": host, "path": "/"})
            await context.add_cookies(cookies)
        return context

    def _on_response(self, live, response):
        """Passive capture of the list requests the inbox tab makes itself."""
        try:
            if LIST_ENDPOINT not in response.url:
                return
            live.list_url = response.url
            live.capture_seq += 1
            asyncio.ensure_future(self._capture_body(live, response))
        except Exception:
            log.debug("response hook failed", exc_info=True)

    async def _capture_body(self, live, response):
        try:
            if response.ok:
                live.passive.append(await response.text())
        except Exception:
            pass                      # body gone after navigation: harmless

    async def _reload_page(self, live, account, settings, session):
        """Heartbeat: (re)load the inbox tab, renew tokens, persist the session."""
        base_url = settings["base_url"]
        if live.page is None or live.page.is_closed():
            live.page = await live.context.new_page()
            live.page.on("response", lambda response: self._on_response(live, response))

        sequence = live.capture_seq
        await live.page.goto(base_url + settings.get("inbox_path", "/recv.php"),
                             wait_until="domcontentloaded", timeout=45000)
        deadline = time.time() + float(settings.get("capture_timeout", 10))
        while live.capture_seq == sequence and time.time() < deadline:
            await asyncio.sleep(0.2)
        html = await live.page.content()

        cookies = {c["name"]: c["value"] for c in await live.context.cookies(base_url)}
        session_hash = _session_from_cookies(cookies) or session.get("session_hash", "")
        if not session_hash:
            raise LoginRequired(
                "m.kuku.lu set no session cookie (the session has expired or was "
                "logged out). Re-link %s from Accounts." % account["email"])
        csrf = cookies.get("cookie_csrf_token", "") or session.get("csrf_token", "")

        # Persist the renewed session (heartbeat write).
        try:
            state = await live.context.storage_state()
            self.db.update_instaddr_runtime(
                account["id"], storage_state=json.dumps(state),
                session_hash=session_hash, csrf_token=csrf or None)
            live.opened_hash = session_hash      # our own write is not a re-link
        except Exception:
            log.debug("could not persist renewed session for %s", account["email"],
                      exc_info=True)

        if live.capture_seq == sequence:
            # The page made no list request of its own: build one.
            if not csrf:
                raise RuntimeError("inbox page made no list request and set no CSRF cookie; "
                                   "check instaddr.browser.inbox_path")
            params = {"nopost": "1", "csrf_token_check": csrf}
            match = SUBTOKEN_RE.search(html)
            if match:
                params["csrf_subtoken_check"] = match.group(1)
            live.list_url = "%s/%s?%s" % (base_url, LIST_ENDPOINT, urlencode(params))
        live.last_reload = time.time()
        live.force_reload = False
        live.empty_streak = 0

    async def _page_fetch(self, live, url, form=None):
        """Fetch from inside the inbox tab (browser TLS, cookies and origin)."""
        result = await live.page.evaluate(
            """async ([url, body]) => {
                const options = {credentials: 'include', cache: 'no-store'};
                if (body !== null) {
                    options.method = 'POST';
                    options.headers = {'Content-Type': 'application/x-www-form-urlencoded'};
                    options.body = body;
                }
                const response = await fetch(url, options);
                return {status: response.status, text: await response.text()};
            }""", [url, urlencode(form) if form is not None else None])
        if result["status"] >= 400:
            raise RuntimeError("HTTP %s for %s" % (result["status"], url.split("?")[0]))
        return result["text"]

    async def _fetch_text(self, live, url, form=None):
        """GET/POST through the browser context; fall back to an in-tab fetch."""
        try:
            if form is None:
                response = await live.context.request.get(url, timeout=30000)
            else:
                response = await live.context.request.post(url, timeout=30000, form=form)
            if response.ok:
                return await response.text()
            first_error = RuntimeError("HTTP %s for %s" % (response.status, url.split("?")[0]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            first_error = exc
        if live.page is None or live.page.is_closed():
            raise first_error
        return await self._page_fetch(live, url, form)

    async def _fetch_list(self, live, list_url):
        return await self._fetch_text(live, with_query(list_url, _=int(time.time() * 1000)))

    def _dump(self, account, name, text):
        """Save a raw page for troubleshooting when instaddr.debug_dump is on."""
        if not self.store.get("instaddr", "debug_dump", default=False):
            return
        try:
            folder = os.path.join(os.path.dirname(self.store.path), "debug_out")
            os.makedirs(folder, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", account["email"])[:60]
            with open(os.path.join(folder, "%s-%s.html" % (safe, name)), "w",
                      encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            log.debug("could not write debug dump", exc_info=True)

    async def _sync(self, live, account, settings):
        """One pass: collect list rows (active + passive), store what is new."""
        account_id = account["id"]
        texts = []
        try:
            texts.append(await self._fetch_list(live, live.list_url))
            self._dump(account, "list", texts[0])
        except asyncio.CancelledError:
            raise
        except Exception:
            if not live.passive:
                raise
            log.debug("%s: active list fetch failed, using captured responses",
                      account["email"], exc_info=True)
        while live.passive:
            texts.append(live.passive.popleft())

        items = []
        known = set()
        for text in texts:
            for item in parse_inbox_list(text, settings["list_regex"]):
                if item[0] not in known:
                    known.add(item[0])
                    items.append(item)

        if not items:
            # No rows. For an inbox known to hold mail this means the tokens
            # in the list URL went stale: reload the tab now instead of
            # polling a dead URL until the next scheduled heartbeat.
            live.empty_streak += 1
            had_mail = live.had_items or self.db.instaddr_seen_count(account_id) > 0
            gap = min(300, (20 if had_mail else 120) * (2 ** min(live.empty_reloads, 4)))
            if live.empty_streak >= 2 and time.time() - live.last_reload > gap:
                live.force_reload = True
                live.empty_reloads += 1
            return 0
        live.empty_streak = 0
        live.empty_reloads = 0
        live.had_items = True

        init_key = "instaddr:%s:initialized" % account_id
        initialized = self.db.get_state(init_key, "") == "1"
        unseen = self.db.instaddr_unseen(account_id, [item[0] for item in items])
        pending = [item for item in items if item[0] in unseen]

        if not initialized:
            initial = max(0, int(self.store.get("instaddr", "initial_fetch", default=20)))
            self.db.instaddr_mark_seen(account_id, [item[0] for item in pending[initial:]])
            pending = pending[:initial]
        elif len(pending) == len(items):
            pending.extend(await self._older_pages(live, account, settings,
                                                   {item[0] for item in items}))

        stored = 0
        # Newest first: the code someone is waiting for must not queue behind
        # older mail. Each message is isolated, so one that cannot be opened
        # never holds up the rest.
        for num, key, row_text in pending:
            if self._stopping:
                break
            try:
                if await self._store_message(live, account, settings, num, key, row_text):
                    stored += 1
                live.failures.pop(num, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts = live.failures.get(num, 0) + 1
                live.failures[num] = attempts
                log.warning("%s: message %s could not be read (attempt %d/%d): %s",
                            account["email"], num, attempts, MAX_MESSAGE_ATTEMPTS,
                            (str(exc).splitlines() or [exc.__class__.__name__])[0][:200])
                if attempts >= MAX_MESSAGE_ATTEMPTS:
                    log.exception("%s: giving up on body of message %s; storing the "
                                  "list row instead", account["email"], num)
                    if self._store_row_only(account, num, row_text, exc):
                        stored += 1
                    live.failures.pop(num, None)

        if not initialized and not self._stopping:
            self.db.set_state(init_key, "1")
        return stored

    async def _store_message(self, live, account, settings, num, key, row_text):
        base_url = settings["base_url"]
        html = await self._fetch_text(live, base_url + VIEW_ENDPOINT,
                                      form={"num": num, "key": key, "noscroll": "1"})
        self._dump(account, "view-latest", html)
        parsed = parse_message_view(html, row_text, settings.get("body_selector", ""))

        # Body served from a separate frame URL: fetch that document.
        if parsed["body_frame_url"]:
            try:
                frame_html = await self._fetch_text(
                    live, urljoin(base_url + "/", parsed["body_frame_url"]))
                self._dump(account, "frame-latest", frame_html)
                if body_has_content(frame_html):
                    parsed["body_html"] = frame_html
                    parsed["body_text"] = html_to_text(frame_html)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("%s: could not load body frame of message %s",
                            account["email"], num, exc_info=True)

        if body_has_content(parsed["body_html"]):
            parsed["body_html"] = await self._inline_site_images(live, base_url,
                                                                 parsed["body_html"])
        else:
            # Never store an empty message: keep every bit of text the page
            # showed so the code is still readable and searchable.
            if not live.empty_body_warned:
                live.empty_body_warned = True
                log.warning(
                    "%s: no body container found for message %s. Turn on 'Save raw "
                    "InstAddr pages' in Accounts and check debug_out, or set "
                    "instaddr.body_selector.", account["email"], num)
            parsed["body_html"] = ""
            parsed["body_text"] = parsed["page_header_text"] or html_to_text(html)

        received_at = estimate_received_at(row_text, parsed["page_header_text"])
        address = parsed["recipient"] or account["email"]
        row_id = self.ingestor.ingest_fields(
            platform="instaddr", account_email=address, recipient=address,
            sender=parsed["sender"], subject=parsed["subject"],
            body_text=parsed["body_text"], body_html=parsed["body_html"],
            received_at=received_at, dedupe_key="instaddr-num:%s" % num,
            raw_headers=build_headers(parsed, received_at, num, row_text))
        self.db.instaddr_mark_seen(account["id"], [num])
        return row_id is not None

    def _store_row_only(self, account, num, row_text, error):
        """Last resort for a message whose page cannot be read: keep the row."""
        sender, recipient = parse_header_line(row_text)
        address = recipient or account["email"]
        received_at = estimate_received_at(row_text)
        parsed = {"sender": sender, "recipient": address,
                  "subject": row_subject(row_text) or "(message body unavailable)",
                  "page_header_text": ""}
        note = ("The body of this message could not be read from m.kuku.lu (%s: %s).\n"
                "Shown below is its inbox row.\n\n%s"
                % (error.__class__.__name__, (str(error).splitlines() or [""])[0][:200],
                   row_text))
        row_id = self.ingestor.ingest_fields(
            platform="instaddr", account_email=address, recipient=address,
            sender=sender, subject=parsed["subject"], body_text=note,
            received_at=received_at, dedupe_key="instaddr-num:%s" % num,
            raw_headers=build_headers(parsed, received_at, num, row_text))
        self.db.instaddr_mark_seen(account["id"], [num])
        return row_id is not None

    async def _inline_site_images(self, live, base_url, body_html):
        """Make images that only load with the m.kuku.lu session self-contained.

        Images hosted by the sender load in the dashboard as they are.
        Images served by m.kuku.lu itself (relative URLs, attachment or
        proxy endpoints) need the session cookie, so they are downloaded
        here and embedded as data: URIs.
        """
        if not re.search(r"<img\b", body_html, re.IGNORECASE):
            return body_html
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(body_html, "html.parser")
        base_host = urlsplit(base_url).netloc.lower()
        changed = False
        fetched = 0
        for img in soup.find_all("img"):
            src = (img.get("src") or "").strip()
            if not src or src.lower().startswith(("data:", "cid:", "blob:", "mailto:")):
                continue
            if src.startswith("//"):
                absolute = "https:" + src
            elif re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", src):
                absolute = src
            else:
                absolute = urljoin(base_url + "/", src)
            if urlsplit(absolute).netloc.lower() != base_host:
                if absolute != src:
                    img["src"] = absolute
                    changed = True
                continue
            if fetched >= MAX_INLINE_IMAGES:
                continue
            try:
                response = await live.context.request.get(absolute, timeout=20000)
                content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
                data = await response.body() if response.ok else b""
                if data and len(data) <= MAX_INLINE_IMAGE_BYTES and content_type.startswith("image/"):
                    img["src"] = "data:%s;base64,%s" % (
                        content_type, base64.b64encode(data).decode("ascii"))
                    changed = True
                    fetched += 1
                elif absolute != src:
                    img["src"] = absolute
                    changed = True
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("could not inline image %s", absolute, exc_info=True)
        return str(soup) if changed else body_html

    async def _older_pages(self, live, account, settings, known_nums):
        page_param = settings.get("page_param", "")
        if not page_param:
            if len(known_nums) >= GAP_WARNING_MIN_ROWS and not live.gap_warned:
                live.gap_warned = True
                log.warning(
                    "%s: a full list page (%d rows) was new since the last poll; older "
                    "mail may be on page 2. Set instaddr.browser.page_param.",
                    account["email"], len(known_nums))
            return []
        extra = []
        base = int(settings.get("page_base", 0))
        for step in range(1, int(settings.get("max_pages", 50))):
            url = with_query(live.list_url, **{page_param: base + step})
            items = parse_inbox_list(await self._fetch_list(live, url), settings["list_regex"])
            fresh = [item for item in items if item[0] not in known_nums]
            if not fresh:
                break
            known_nums.update(item[0] for item in fresh)
            unseen = self.db.instaddr_unseen(account["id"], [item[0] for item in fresh])
            extra.extend(item for item in fresh if item[0] in unseen)
            if len(unseen) < len(fresh):
                break
        return extra


class _Live:
    """Per-account runtime state of the open inbox tab."""

    def __init__(self):
        self.context = None
        self.page = None
        self.opened_hash = ""
        self.list_url = ""
        self.capture_seq = 0
        self.last_reload = 0.0
        self.force_reload = False
        self.passive = collections.deque(maxlen=20)
        self.failures = {}
        self.empty_streak = 0
        self.empty_reloads = 0
        self.had_items = False
        self.gap_warned = False
        self.empty_body_warned = False


async def _safe_close(context):
    try:
        await context.close()
    except Exception:
        pass


def _load_storage_state(raw):
    """Parse a storage_state JSON string from the DB into a dict, or None."""
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(state, dict) and (state.get("cookies") or state.get("origins")):
        return state
    return None
