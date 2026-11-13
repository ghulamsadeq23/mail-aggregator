"""InstAddr (m.kuku.lu) Method B: session-cookie inbox poller.

InstAddr has no official API. This worker reuses the cookies of a
browser session that is already logged in to the account and calls the
same endpoints the web inbox uses:

  GET  /recv._ajax.php?nopost=1&csrf_token_check=<csrf>   inbox list
  POST /smphone.app.recv.view.php  (num, key, noscroll=1) one message

The page markup is not a stable contract. The parsing below is
deliberately tolerant, the list pattern can be overridden with
instaddr.list_regex in config.json, and tools/instaddr_debug.py dumps
the raw responses so the patterns can be adjusted if the site changes.
"""

import json
import logging
import re
import threading
import time

import requests
from bs4 import BeautifulSoup

from .extractor import html_to_text

log = logging.getLogger("instaddr")

DEFAULT_LIST_REGEX = r"openMailData\(\s*'(\d+)'\s*,\s*'([0-9A-Za-z]+)'"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# The m.kuku.lu summary line puts sender and recipient around an arrow:
#   "04:06 (22min) | Stake.com <noreply@mail.stake.com> >> andbadend@lamha.pl"
# The real glyph is U+00BB; "&raquo;", "->" and the full-width arrows are
# accepted too so the split survives small markup changes.
ARROW_RE = re.compile(r"\s*(?:\u00bb|\u279c|\u2192|\uff1e|&raquo;|-+>|=+>)\s*")
# VERP / Return-Path bounce address: "bounces+15421720-b1e0-siradd=lamha.pl@..."
# The real recipient is encoded as "<local>=<domain>" in the local part.
# group 1 excludes "+" and "-" so it stops at the VERP separator and keeps
# only the final "<local>=<domain>" pair, e.g. "...-siradd=lamha.pl@".
VERP_RE = re.compile(r"([A-Za-z0-9._%]+)=([A-Za-z0-9.\-]+\.[A-Za-z]{2,})@")
# Labels: From / Japanese "sashidashinin" / "soushinsha".
FROM_LABEL_RE = re.compile(
    r"(?:From|\u5dee\u51fa\u4eba|\u9001\u4fe1\u8005)\s*[:\uff1a]\s*([^\n]{0,160})",
    re.IGNORECASE)
# Labels: To / Japanese "atesaki" / "jushin address".
TO_LABEL_RE = re.compile(
    r"(?:\bTo\b|\u5b9b\u5148|\u53d7\u4fe1\u30a2\u30c9\u30ec\u30b9)\s*[:\uff1a]\s*([^\n]{0,160})",
    re.IGNORECASE)
# m.kuku.lu page chrome. Applied ONLY when no body wrapper can be isolated
# and the whole view page must be cleaned; never inside an isolated email.
JUNK_SELECTOR_RE = re.compile(
    r"tool\s*bar|toolbar|menu|navi|nav_|_nav|header|footer|reply|delete|trash|"
    r"forward|resend|btn|button|operation|mailmenu|listarea|mail_list|adbox|"
    r"advertis|banner|pankuzu|breadcrumb|snsarea|share",
    re.IGNORECASE)
MAX_BACKOFF = 300


def _clean_address(text):
    """Trim a raw address fragment, keeping a display name if present."""
    text = re.sub(r"\s+", " ", (text or "").strip()).strip("|-\u2013 ")
    # Drop a leading "HH:MM (age)" timestamp the list line carries.
    text = re.sub(r"^\d{1,2}:\d{2}\s*(?:\([^)]*\))?\s*[|\-\u2013]?\s*", "", text)
    return text.strip()


def verp_recipient(text):
    """Pull "user@domain" out of a VERP bounce address, or ''."""
    best = ""
    for match in VERP_RE.finditer(text or ""):
        local, domain = match.group(1), match.group(2)
        # Skip the VERP bookkeeping prefixes (bounces+..., prvs=..., srs0=).
        if local.lower() in ("bounces", "bounce", "prvs", "prvs1", "srs0", "srs1", "msprvs1"):
            continue
        best = (local + "@" + domain).lower()
    return best


def parse_header_line(text):
    """Return (sender, recipient) from a "<sender> >> <recipient>" line.

    Falls back to decoding a VERP bounce address for the recipient. Either
    field may come back empty when the line does not carry it.
    """
    sender = ""
    recipient = ""
    for raw_line in (text or "").splitlines():
        if not ARROW_RE.search(raw_line):
            continue
        parts = ARROW_RE.split(raw_line, maxsplit=1)
        if len(parts) < 2:
            continue
        left, right = parts[0], parts[1]
        left_clean = _clean_address(left)
        right_clean = _clean_address(right)
        if EMAIL_RE.search(left_clean) or "<" in left_clean:
            sender = left_clean
        right_match = EMAIL_RE.search(right_clean)
        if right_match:
            recipient = right_match.group(0).lower()
        if sender or recipient:
            break
    if not recipient:
        recipient = verp_recipient(text)
    return sender, recipient


def parse_inbox_list(html, list_regex=""):
    """Return [(num, key, row_text)] found in the inbox list response."""
    pattern = re.compile(list_regex or DEFAULT_LIST_REGEX)
    matches = list(pattern.finditer(html))
    items = []
    seen = set()
    for index, match in enumerate(matches):
        num, key = match.group(1), match.group(2)
        if num in seen:
            continue
        seen.add(num)
        end = matches[index + 1].start() if index + 1 < len(matches) else match.end() + 3000
        row_html = html[max(0, match.start() - 300):min(len(html), end)]
        items.append((num, key, html_to_text(row_html)))
    return items


BODY_ID_RE = re.compile(
    r"area[-_]?data|mail[-_]?body|mailbody|honbun|message[-_]?body", re.IGNORECASE)
# Attributes webmail pages use to park the real image URL until the user
# clicks "show images".
LAZY_IMG_ATTRS = ("data-src", "data-original", "data-orig-src", "data-url",
                  "original-src", "originalsrc", "_src", "data-lazy-src")
AGE_RE = re.compile(
    r"\(\s*(\d+)\s*(sec|second|min|minute|hour|hr|day|week|month|year|"
    r"\u79d2|\u5206|\u6642\u9593|\u65e5|\u9031\u9593|\u9031|\u30f6\u6708|\u304b\u6708|\u5e74)"
    r"[a-z]*[^)]*\)", re.IGNORECASE)
AGE_SECONDS = {
    "sec": 1, "second": 1, "\u79d2": 1,
    "min": 60, "minute": 60, "\u5206": 60,
    "hour": 3600, "hr": 3600, "\u6642\u9593": 3600,
    "day": 86400, "\u65e5": 86400,
    "week": 604800, "\u9031\u9593": 604800, "\u9031": 604800,
    "month": 2592000, "\u30f6\u6708": 2592000, "\u304b\u6708": 2592000,
    "year": 31536000, "\u5e74": 31536000,
}
CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
ABS_DATE_RE = re.compile(
    r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?")


def _richness(node):
    """Rough amount of real content in a node (text + media)."""
    if node is None:
        return 0
    text = node.get_text(" ", strip=True)
    media = len(node.find_all(["img", "table", "a"])) if hasattr(node, "find_all") else 0
    return len(text) + 40 * media


def _find_body_container(soup, body_selector=""):
    """Locate the element that wraps the email itself.

    Returns (container, isolated). Candidates, in order of trust:
      1. instaddr.body_selector from config (always wins)
      2. an <iframe srcdoc> (holds the original HTML verbatim)
      3. the first dir="ltr" block (the wrapper m.kuku.lu renders mail in)
      4. an id such as #area_data / #mail_body
    3 and 4 are cross-checked, because either can be the wrong one on a
    given page: the id block may hold only the header, and a dir="ltr"
    block may be a fragment inside the email. The one with real content
    wins; when one contains the other, the tighter one is used unless it
    holds well under the bulk of the content.
    """
    if body_selector:
        try:
            found = soup.select_one(body_selector)
        except Exception:
            found = None
        if found is not None:
            return found, True
    for frame in soup.find_all("iframe"):
        if frame.get("srcdoc"):
            return frame, True

    ltr = soup.find(attrs={"dir": "ltr"})
    by_id = soup.find(id=BODY_ID_RE)
    if ltr is not None and by_id is not None and ltr is not by_id:
        ltr_score, id_score = _richness(ltr), _richness(by_id)
        if by_id in ltr.parents:                      # ltr sits inside the id block
            return (ltr if ltr_score >= 0.6 * id_score else by_id), True
        if ltr in by_id.parents:                      # id block sits inside ltr
            return (by_id if id_score >= 0.6 * ltr_score else ltr), True
        return (ltr if ltr_score >= id_score else by_id), True
    if ltr is not None:
        return ltr, True
    if by_id is not None:
        return by_id, True
    return (soup.body or soup), False


def _restore_lazy_images(container):
    """Give <img> tags their real URL back when the page parked it elsewhere."""
    for img in container.find_all("img"):
        if not isinstance(getattr(img, "attrs", None), dict):
            continue
        src = (img.get("src") or "").strip()
        if src and not src.startswith("data:image/gif;base64,R0lGODlhAQAB") and "blank" not in src:
            continue
        for name in LAZY_IMG_ATTRS:
            if img.get(name):
                img["src"] = img[name]
                break


def extract_body_container(soup, body_selector=""):
    """Return the HTML of just the email body, without m.kuku.lu page chrome.

    Once the body wrapper is isolated, its content is the sender's own
    markup and is returned untouched: emails legitimately use ids/classes
    such as header, footer, banner, btn, menu or share, plus forms and
    buttons, and removing any of them wrecks table layouts. Only <script>
    and <noscript> are dropped.

    Chrome stripping by id/class is a last resort, used only when no body
    wrapper exists and the whole view page has to be cleaned.
    """
    container, isolated = _find_body_container(soup, body_selector)

    # An <iframe srcdoc="..."> already holds the original mail HTML verbatim.
    if getattr(container, "name", "") == "iframe" and container.get("srcdoc"):
        return container["srcdoc"]

    import copy
    container = copy.copy(container)
    for tag in list(container.find_all(["script", "noscript"])):
        if not isinstance(getattr(tag, "attrs", None), dict):
            continue                      # already destroyed with its parent
        tag.decompose()
    _restore_lazy_images(container)

    if not isolated:
        # decompose() clears the attrs of every descendant it destroys, so
        # iterate over a snapshot and skip tags that are already gone.
        for tag in list(container.find_all(True)):
            if not isinstance(getattr(tag, "attrs", None), dict):
                continue
            attrs = tag.attrs
            classes = attrs.get("class") or []
            if isinstance(classes, str):
                classes = [classes]
            marker = " ".join(filter(None, [
                str(attrs.get("id") or ""), " ".join(classes), str(attrs.get("role") or "")]))
            if marker and JUNK_SELECTOR_RE.search(marker):
                tag.decompose()
    return str(container)


def body_has_content(body_html):
    """True when the extracted body shows something (text or an image)."""
    if not body_html:
        return False
    if re.search(r"<img\b", body_html, re.IGNORECASE):
        return True
    return len(html_to_text(body_html).strip()) >= 2


def find_body_frame_url(soup):
    """URL of an <iframe src> that loads the mail body separately, or ''."""
    for frame in soup.find_all("iframe"):
        src = (frame.get("src") or "").strip()
        if src and not src.lower().startswith(("about:", "javascript:", "data:")):
            return src
    return ""


def row_subject(row_text):
    """Subject line from an inbox-list row (the line that is not the header)."""
    for line in (row_text or "").splitlines():
        line = line.strip()
        if not line or ARROW_RE.search(line) or "openMailData" in line:
            continue
        if EMAIL_RE.fullmatch(line) or AGE_RE.fullmatch(line):
            continue
        return line[:300]
    return ""


def estimate_received_at(row_text, view_text="", now=None):
    """Best UTC timestamp for a message, as ISO 8601.

    The list row shows a clock time and an age ("04:06 (22min)") in the
    site's display time zone, which is unknown here. The age gives the
    moment to within its unit; the clock's minutes then pin it down to the
    minute. A full date on the view page is used when present, with the
    zone offset derived from the age. With no hints the mail is new: now.
    """
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc)
    age_match = AGE_RE.search(row_text or "")
    if not age_match:
        return now.strftime("%Y-%m-%dT%H:%M:%SZ")
    unit = AGE_SECONDS.get(age_match.group(2).lower(), 60)
    count = int(age_match.group(1))
    # "(2hour)" means between 2 and 3 hours: aim for the middle of the unit.
    approx = now - timedelta(seconds=count * unit + (unit / 2.0 if unit >= 3600 else 0))

    newest = now - timedelta(seconds=count * unit)
    oldest = now - timedelta(seconds=(count + 1) * unit)

    # A full date on the view page: derive the display zone from the age.
    # Only possible while the age is precise enough (minutes or hours).
    absolute = ABS_DATE_RE.search(view_text or "")
    if absolute and unit <= 3600:
        try:
            parts = [int(g) if g else 0 for g in absolute.groups()]
            shown = datetime(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5],
                             tzinfo=timezone.utc)
            step = 3600.0 if unit == 3600 else 900.0
            offset = round((shown - approx).total_seconds() / step) * step
            if abs(offset) <= 14 * 3600:
                exact = shown - timedelta(seconds=offset)
                return min(exact, now).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass

    # Clock minutes from the row pin the time down inside the age window.
    clock = CLOCK_RE.search((row_text or "")[:age_match.start()] or (row_text or ""))
    if clock and unit <= 3600 and int(clock.group(2)) < 60:
        minute = int(clock.group(2))
        best = None
        for shift in (-1, 0, 1):
            option = (approx + timedelta(hours=shift)).replace(
                minute=minute, second=0, microsecond=0)
            inside = oldest - timedelta(seconds=90) <= option <= newest + timedelta(seconds=90)
            distance = abs((option - approx).total_seconds())
            rank = (0 if inside else 1, distance)
            if best is None or rank < best[0]:
                best = (rank, option, distance)
        # Only trust the clock when it lands inside the age window. (A
        # display zone with a 30/45-minute offset would not; the age-based
        # estimate is kept then.)
        if best[0][0] == 0:
            approx = best[1]
    return min(approx, now).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_message_view(html, row_text="", body_selector=""):
    """Extract sender, recipient, subject, headers and body from the view page."""
    soup = BeautifulSoup(html, "html.parser")

    # Subject: the dedicated element, else the list row, else the page title.
    subject = ""
    subject_el = soup.find(class_="full")
    if subject_el is not None:
        subject = subject_el.get_text(" ", strip=True)
    if not subject:
        subject = row_subject(row_text)
    if not subject and soup.title is not None:
        subject = soup.title.get_text(" ", strip=True)

    container, _isolated = _find_body_container(soup, body_selector)
    body_html = extract_body_container(soup, body_selector)
    body_text = html_to_text(body_html)

    # Everything on the page that is not the body: sender line, banner, dates.
    import copy
    chrome = copy.copy(soup)
    try:
        twin, _ = _find_body_container(chrome, body_selector)
        if twin is not None and twin is not chrome and getattr(twin, "name", None) not in (None, "[document]", "body"):
            twin.decompose()
    except Exception:
        pass
    page_header_text = html_to_text(str(chrome))[:4000]
    header_text = (page_header_text + "\n" + html_to_text(html)[:1500])

    # 1) The list row already carries the "<sender> >> <recipient>" line.
    sender, recipient = parse_header_line(row_text)
    # 2) The view page's own header and banner refine or fill the gaps.
    page_sender, page_recipient = parse_header_line(header_text)
    sender = sender or page_sender
    recipient = recipient or page_recipient

    # 3) Explicit "From:" / "To:" labels, if the page uses them.
    if not sender:
        match = FROM_LABEL_RE.search(header_text)
        if match and EMAIL_RE.search(match.group(1)):
            sender = _clean_address(match.group(1))
    if not recipient:
        match = TO_LABEL_RE.search(header_text)
        if match:
            found = EMAIL_RE.search(match.group(1))
            if found:
                recipient = found.group(0).lower()

    # 4) VERP banner fallback for the recipient only.
    if not recipient:
        recipient = verp_recipient(row_text + "\n" + header_text)

    # 5) Last resort: pick distinct addresses out of the text.
    if not sender or not recipient:
        addresses = []
        for address in EMAIL_RE.findall(row_text + "\n" + header_text):
            if address.lower() not in [a.lower() for a in addresses]:
                addresses.append(address)
        if not sender and addresses:
            sender = addresses[0]
        if not recipient:
            for address in addresses:
                if address.lower() not in (sender or "").lower():
                    recipient = address.lower()
                    break

    # A body served from its own frame document: the frame inside the body
    # wrapper is the message; any frame on the page is tried when the
    # wrapper shows nothing.
    frame_url = ""
    if getattr(container, "find_all", None) is not None and getattr(container, "name", "") != "iframe":
        frame_url = find_body_frame_url(container)
    if not frame_url and not body_has_content(body_html):
        frame_url = find_body_frame_url(soup)

    return_path = ""
    for match in re.finditer(r"[A-Za-z0-9._%+\-=]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", header_text):
        if "=" in match.group(0) or match.group(0).lower().startswith(("bounce", "prvs", "srs")):
            return_path = match.group(0)
            break

    return {
        "sender": sender,
        "recipient": recipient,
        "subject": subject,
        "body_text": body_text,
        "body_html": body_html,
        "body_frame_url": frame_url,
        "return_path": return_path,
        "page_header_text": page_header_text,
    }


def build_headers(parsed, received_at, num="", row_text=""):
    """Readable header block for an InstAddr message (the site exposes no
    raw RFC 822 headers, so this records everything the pages do show)."""
    lines = [
        "From: %s" % (parsed.get("sender") or ""),
        "To: %s" % (parsed.get("recipient") or ""),
        "Subject: %s" % (parsed.get("subject") or ""),
        "Date: %s" % received_at,
    ]
    if parsed.get("return_path"):
        lines.append("Return-Path: <%s>" % parsed["return_path"])
    if num:
        lines.append("X-InstAddr-Message-Number: %s" % num)
    if row_text:
        lines.append("X-InstAddr-List-Row: %s" % " | ".join(
            part.strip() for part in row_text.splitlines() if part.strip())[:500])
    if parsed.get("page_header_text"):
        lines.append("")
        lines.append("--- text shown around the message on m.kuku.lu ---")
        lines.append(parsed["page_header_text"])
    return "\n".join(lines)


class InstAddrClient:
    """Thin HTTP client around the InstAddr web inbox."""

    def __init__(self, base_url, session_key, csrf_token="", user_agent=""):
        self.base_url = base_url.rstrip("/")
        self.host = self.base_url.split("//", 1)[-1].split("/", 1)[0]
        self.session = requests.Session()
        if user_agent:
            self.session.headers["User-Agent"] = user_agent
        self.session.headers["Accept-Language"] = "en-US,en;q=0.9,ja;q=0.8"
        self.session.cookies.set("cookie_sessionhash", session_key, domain=self.host)
        self.csrf_token = csrf_token
        if csrf_token:
            self.session.cookies.set("cookie_csrf_token", csrf_token, domain=self.host)

    def ensure_csrf(self):
        if self.csrf_token:
            return
        response = self.session.get(self.base_url + "/", timeout=30)
        response.raise_for_status()
        token = self.session.cookies.get("cookie_csrf_token", domain=self.host)
        if not token:
            token = self.session.cookies.get("cookie_csrf_token")
        if not token:
            raise RuntimeError("no cookie_csrf_token issued; set csrf_token for this account")
        self.csrf_token = token

    def fetch_list_html(self):
        self.ensure_csrf()
        response = self.session.get(
            self.base_url + "/recv._ajax.php",
            params={"nopost": "1", "csrf_token_check": self.csrf_token,
                    "_": str(int(time.time() * 1000))},
            timeout=30)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        return response.text

    def fetch_view_html(self, num, key):
        response = self.session.post(
            self.base_url + "/smphone.app.recv.view.php",
            data={"num": num, "key": key, "noscroll": "1"},
            timeout=30)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        return response.text


class InstAddrWorker(threading.Thread):
    def __init__(self, account, store, db, ingestor, status_cb):
        super().__init__(name="instaddr-%s" % account["email"], daemon=True)
        self.account = account
        self.store = store
        self.db = db
        self.ingestor = ingestor
        self.status_cb = status_cb
        self.stop_event = threading.Event()
        self.state_key = "instaddr:%s:seen" % account["id"]

    def stop(self):
        self.stop_event.set()

    def _migrate_legacy_state(self):
        """Move the old JSON seen-list into the instaddr_seen table."""
        raw = self.db.get_state(self.state_key, "")
        if not raw:
            return
        try:
            self.db.instaddr_mark_seen(self.account["id"], list(json.loads(raw)))
            self.db.set_state("instaddr:%s:initialized" % self.account["id"], "1")
        except ValueError:
            pass
        self.db.set_state(self.state_key, "")

    def run(self):
        settings = self.store.get("instaddr", default={})
        client = InstAddrClient(
            settings.get("base_url", "https://m.kuku.lu"),
            self.account["session_key"],
            self.account["csrf_token"],
            settings.get("user_agent", ""))
        backoff = 10
        self.status_cb(self.account["id"], "connecting", "")
        while not self.stop_event.is_set():
            try:
                new_count = self._poll_once(client)
                self.status_cb(self.account["id"], "ok", "", new_count)
                backoff = 10
                interval = max(5, int(self.store.get("instaddr", "poll_interval", default=12)))
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                log.warning("%s: %s (retry in %ss)", self.account["email"], message, backoff)
                self.status_cb(self.account["id"], "error", message[:300])
                interval = backoff
                backoff = min(backoff * 2, MAX_BACKOFF)
            if self.stop_event.wait(interval):
                break
        self.status_cb(self.account["id"], "stopped", "")

    def _poll_once(self, client):
        list_regex = self.store.get("instaddr", "list_regex", default="")
        items = parse_inbox_list(client.fetch_list_html(), list_regex)

        account_id = self.account["id"]
        self._migrate_legacy_state()
        init_key = "instaddr:%s:initialized" % account_id
        initialized = self.db.get_state(init_key, "") == "1"
        unseen = self.db.instaddr_unseen(account_id, [item[0] for item in items])
        pending = [item for item in items if item[0] in unseen]

        if not initialized:
            # The list is newest-first: import a few, remember the rest.
            initial = max(0, int(self.store.get("instaddr", "initial_fetch", default=20)))
            self.db.instaddr_mark_seen(account_id, [item[0] for item in pending[initial:]])
            pending = pending[:initial]

        stored = 0
        for num, key, row_text in reversed(pending):
            if self.stop_event.is_set():
                break
            parsed = parse_message_view(client.fetch_view_html(num, key), row_text)
            row_id = self.ingestor.ingest_fields(
                platform="instaddr",
                account_email=parsed["recipient"] or self.account["email"],
                recipient=parsed["recipient"] or self.account["email"],
                sender=parsed["sender"],
                subject=parsed["subject"],
                body_text=parsed["body_text"],
                body_html=parsed["body_html"],
                dedupe_key="instaddr-num:%s" % num)
            # Mark seen only after the message is stored.
            self.db.instaddr_mark_seen(account_id, [num])
            if row_id is not None:
                stored += 1
            time.sleep(0.3)

        if not initialized and not self.stop_event.is_set():
            self.db.set_state(init_key, "1")
        return stored
