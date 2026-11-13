"""Body cleaning plus OTP and verification-link extraction."""

import re

from bs4 import BeautifulSoup

# Keyword -> weight. Higher weight means stronger evidence that a nearby
# number is a one-time code. Non-ASCII keywords are written as escapes.
OTP_KEYWORDS = {
    "verification code": 10,
    "security code": 10,
    "confirmation code": 10,
    "authentication code": 10,
    "auth code": 9,
    "login code": 9,
    "sign-in code": 9,
    "sign in code": 9,
    "access code": 9,
    "one-time": 9,
    "one time": 9,
    "passcode": 9,
    "pass code": 9,
    "otp": 9,
    "2fa": 8,
    "two-factor": 8,
    "verify": 6,
    "verification": 6,
    "pin": 5,
    "code": 5,
    "\u8a8d\u8a3c\u30b3\u30fc\u30c9": 10,   # Japanese: authentication code
    "\u78ba\u8a8d\u30b3\u30fc\u30c9": 10,   # Japanese: confirmation code
    "\u30ef\u30f3\u30bf\u30a4\u30e0": 9,    # Japanese: one-time
    "\u9a8c\u8bc1\u7801": 10,               # Chinese: verification code
    "\u9a57\u8b49\u78bc": 10,               # Chinese (traditional)
}

_KEYWORD_RE = re.compile(
    "|".join(re.escape(k) for k in sorted(OTP_KEYWORDS, key=len, reverse=True)),
    re.IGNORECASE)

# 4-8 digits, or 3+3 digits split by a space or dash, optionally with a
# short letter prefix such as "G-123456". Lookarounds reject dates,
# prices, phone numbers and digits inside longer tokens.
_NUMERIC_RE = re.compile(
    r"(?<![\w.,/#@+-])(?:[A-Z]{1,3}-)?(\d{3}[ -]\d{3}|\d{4,8})(?![\w@/+-]|[.,:]\d)")

# "code: AB12CD" style alphanumeric codes.
_ALNUM_RE = re.compile(
    r"(?:code|otp|passcode|pin)(?:\s+is)?\s*[:\-]?\s*\b([A-Z0-9]{5,10})\b")

_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]\}]+", re.IGNORECASE)

LINK_POSITIVE = {
    "verify": 10, "verification": 10, "confirm": 10, "activate": 10,
    "activation": 10, "validate": 9, "magic": 9, "reset": 8,
    "password": 6, "token": 6, "signin": 6, "sign-in": 6, "login": 6,
    "auth": 5, "invite": 5, "accept": 5, "approve": 5, "unlock": 5,
    "click here": 4, "get started": 3, "complete": 3,
}

LINK_NEGATIVE = (
    "unsubscribe", "privacy", "terms", "preferences", "optout", "opt-out",
    "facebook.com", "twitter.com", "x.com/", "instagram.com", "linkedin.com",
    "youtube.com", "apps.apple.com", "play.google.com", "/help", "/support",
    "/legal", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".css",
)


BLOCK_TAGS = ("p", "div", "tr", "li", "ul", "ol", "table", "blockquote", "pre",
              "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
              "footer", "hr")


def html_to_text(html):
    """Convert HTML to readable plain text."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in list(soup(["script", "style", "head", "title", "meta", "noscript"])):
        # decompose() clears the attrs of the children it destroys; a child
        # still in this snapshot must be skipped, not decomposed twice.
        if not isinstance(getattr(tag, "attrs", None), dict):
            continue
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for cell in soup.find_all(["td", "th"]):
        cell.append(" ")
    for block in soup.find_all(BLOCK_TAGS):
        block.insert_before("\n")
        block.append("\n")
    text = soup.get_text()
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.splitlines()]
    cleaned = []
    blank_run = 0
    for line in lines:
        if line:
            if blank_run >= 1 and cleaned:
                cleaned.append("")
            cleaned.append(line)
            blank_run = 0
        else:
            blank_run += 1
    return "\n".join(cleaned).strip()


def _score_numeric(text, base_bonus):
    """Yield (score, code) for every plausible numeric code in text."""
    keywords = [(m.start(), m.end(), OTP_KEYWORDS[m.group(0).lower()])
                for m in _KEYWORD_RE.finditer(text)]
    if not keywords:
        return
    for match in _NUMERIC_RE.finditer(text):
        digits = re.sub(r"[ -]", "", match.group(1))
        start, end = match.start(), match.end()
        best = None
        for k_start, k_end, weight in keywords:
            if k_end <= start:
                distance = start - k_end
                window = 120
            elif k_start >= end:
                distance = k_start - end
                window = 60
            else:
                continue
            if distance > window:
                continue
            score = weight - distance / 25.0
            if best is None or score > best:
                best = score
        if best is None:
            continue
        if len(digits) == 4 and 1900 <= int(digits) <= 2100:
            best -= 6
        if len(digits) == 6:
            best += 2
        yield best + base_bonus, digits


def extract_otp(subject, body_text):
    """Return the most likely one-time code, or an empty string."""
    candidates = []
    candidates.extend(_score_numeric(subject or "", 3))
    # Subject is prepended so "Your verification code" in the subject
    # still counts when the body only says "Use 482913 to sign in".
    candidates.extend(_score_numeric(
        (subject or "") + "\n" + (body_text or "")[:6000], 0))
    candidates = [c for c in candidates if c[0] > 0]
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    for text in (subject or "", (body_text or "")[:6000]):
        for match in _ALNUM_RE.finditer(text):
            token = match.group(1)
            if any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token):
                return token
    return ""


def _score_link(url, anchor_text):
    lowered = url.lower()
    if not lowered.startswith(("http://", "https://")):
        return -1
    for bad in LINK_NEGATIVE:
        if bad in lowered:
            return -1
    haystack = lowered + " " + (anchor_text or "").lower()
    score = 0
    for word, weight in LINK_POSITIVE.items():
        if word in haystack:
            score += weight
    if score and re.search(r"[A-Za-z0-9_\-]{24,}", url.split("//", 1)[-1]):
        score += 3
    return score


def extract_link(body_text, body_html):
    """Return the most likely verification / action link, or ''."""
    candidates = []
    if body_html:
        soup = BeautifulSoup(body_html, "html.parser")
        for anchor in soup.find_all("a", href=True):
            candidates.append((anchor["href"].strip(), anchor.get_text(" ", strip=True)))
    for match in _URL_RE.finditer(body_text or ""):
        url = match.group(0).rstrip(".,;:!?")
        start = max(0, match.start() - 80)
        candidates.append((url, (body_text or "")[start:match.start()]))

    best_url = ""
    best_score = 0
    for url, context in candidates:
        score = _score_link(url, context)
        if score > best_score:
            best_score = score
            best_url = url
    return best_url


def domain_of(address):
    address = (address or "").strip().lower()
    if "@" not in address:
        return ""
    return address.rsplit("@", 1)[-1].strip("> ")
