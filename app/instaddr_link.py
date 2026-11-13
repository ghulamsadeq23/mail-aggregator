"""Zero-trust InstAddr linking: payload parsing + instant validation.

InstAddr (m.kuku.lu) has no account password in the traditional sense. A
logged-in session is just a volatile cookie (cookie_sessionhash, a.k.a.
shash) that the server maps to temporary storage. "Linking" an account
therefore means: capture that cookie from the user's own browser, clean
it, prove it is live against m.kuku.lu, and store it. This module does the
parsing and the proof; storage lives in the database.

Nothing here raises on bad input: parse_link_payload always returns a dict
so the HTTP handler can give a clean 400 instead of a 500.
"""

import logging
import re
from urllib.parse import unquote

import requests

log = logging.getLogger("instaddr")

SESSION_KEYS = ("session_key", "shash", "cookie_sessionhash", "sessionhash", "session")
CSRF_KEYS = ("csrf_token", "cookie_csrf_token", "csrf")
# A pasted value may be the bare token, "name=value", or a whole cookie
# string. These let us pull the right piece out of any of those.
COOKIE_PAIR_RE = re.compile(r"([A-Za-z0-9_\-]+)\s*=\s*([^;]+)")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._\-]+$")
# A m.kuku.lu session cookie is a long hex-ish string; a login page or an
# anonymous session has none / a short one.
MIN_SESSION_LEN = 8
# Statuses an anti-bot layer returns to non-browser clients regardless of
# whether the session cookie is valid.
BOT_BLOCK_STATUSES = (403, 429, 503)


def _clean(value):
    value = (str(value) if value is not None else "").strip()
    # Strip wrapping quotes a copy-paste often brings along.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def cookies_from_string(text):
    """Parse 'a=1; b=2' (document.cookie / a Cookie header) into a dict."""
    jar = {}
    for match in COOKIE_PAIR_RE.finditer(text or ""):
        name = match.group(1).strip()
        value = unquote(match.group(2).strip().strip('"\''))
        if name and name not in jar:
            jar[name] = value
    return jar


def _extract(data, keys):
    for key in keys:
        if data.get(key):
            return _clean(data[key])
    return ""


def parse_link_payload(data):
    """Normalise any link payload into a clean dict. Never raises.

    Accepts an already-parsed dict (from JSON or a form) and tolerates:
      * a bare token in session_key / shash / cookie_sessionhash
      * a 'name=value' fragment in any of those
      * a whole cookie string in 'cookies' or in the session field itself
    Returns keys: session_hash, csrf_token, user_agent, proxy, email,
    account_id. Missing values come back as ''.
    """
    if not isinstance(data, dict):
        data = {}
    data = {str(k): v for k, v in data.items() if v is not None}

    # A full cookie dump (document.cookie) overrides individual fields.
    jar = {}
    for source in (data.get("cookies"), data.get("cookie"), data.get("document_cookie")):
        if source:
            jar.update(cookies_from_string(str(source)))

    session_hash = _extract(data, SESSION_KEYS)
    csrf_token = _extract(data, CSRF_KEYS)

    # If a session field actually held a cookie string, mine it.
    if session_hash and "=" in session_hash and not TOKEN_RE.match(session_hash):
        mined = cookies_from_string(session_hash)
        jar.update(mined)
        session_hash = ""
    if not session_hash:
        for name in SESSION_KEYS:
            if jar.get(name):
                session_hash = _clean(jar[name])
                break
    if not csrf_token:
        csrf_token = _clean(jar.get("cookie_csrf_token", ""))
    # The cookie is stored URL-encoded in the browser ("SHASH%3A..."); the
    # site and the browser worker expect the decoded form ("SHASH:...").
    if "%" in session_hash:
        session_hash = unquote(session_hash)

    return {
        "session_hash": session_hash,
        "csrf_token": csrf_token,
        "user_agent": _clean(data.get("user_agent") or data.get("useragent") or ""),
        "proxy": _clean(data.get("proxy") or ""),
        "email": _clean(data.get("email") or data.get("label") or "").lower(),
        "account_id": _clean(data.get("account_id") or ""),
    }


def _proxies(proxy_url, host=""):
    if proxy_url:
        return {"http": proxy_url, "https": proxy_url}
    # Loopback targets must not go through an ambient corporate proxy.
    if host.split(":")[0] in ("127.0.0.1", "localhost", "::1"):
        return {"http": None, "https": None}
    return None


def validate_session(base_url, session_hash, csrf_token="", user_agent="", proxy="",
                     timeout=12):
    """Prove a session is live against m.kuku.lu. Never raises.

    Returns a dict: ok (reachable), authenticated (session accepted),
    csrf_token (possibly refreshed), detail (human message). This is the
    instant feedback the dashboard shows right after linking.
    """
    base_url = (base_url or "https://m.kuku.lu").rstrip("/")
    host = base_url.split("//", 1)[-1].split("/", 1)[0]
    cookie_domain = host.split(":")[0]  # a domain must not carry a port
    session = requests.Session()
    if user_agent:
        session.headers["User-Agent"] = user_agent
    session.headers["Accept-Language"] = "en-US,en;q=0.9,ja;q=0.8"
    for name in ("cookie_sessionhash", "shash"):
        session.cookies.set(name, session_hash, domain=cookie_domain)
    if csrf_token:
        session.cookies.set("cookie_csrf_token", csrf_token, domain=cookie_domain)

    try:
        # Touch the inbox page so the server renews/sets the CSRF cookie.
        home = session.get(base_url + "/recv.php", timeout=timeout,
                           proxies=_proxies(proxy, host), allow_redirects=True)
        refreshed_csrf = session.cookies.get("cookie_csrf_token") or csrf_token
        if not refreshed_csrf:
            # Some flows set it only on the root page.
            session.get(base_url + "/", timeout=timeout, proxies=_proxies(proxy, host))
            refreshed_csrf = session.cookies.get("cookie_csrf_token") or ""

        params = {"nopost": "1"}
        if refreshed_csrf:
            params["csrf_token_check"] = refreshed_csrf
        listing = session.get(base_url + "/recv._ajax.php", params=params,
                              timeout=timeout, proxies=_proxies(proxy, host))
    except requests.RequestException as exc:
        reason = str(exc.__class__.__name__)
        if proxy:
            reason += " (via proxy)"
        return {"ok": False, "authenticated": False, "inconclusive": True,
                "csrf_token": csrf_token,
                "detail": "could not reach m.kuku.lu: %s" % reason}

    text = listing.text if listing is not None else ""
    authenticated = True
    detail = "session is live"
    lowered = text.lower()
    if listing.status_code in BOT_BLOCK_STATUSES:
        # Cloudflare fingerprints the TLS handshake of non-browser clients
        # and answers 403 even for a perfectly valid session. That says
        # nothing about the cookie, so this pre-check is inconclusive and
        # the real browser worker gets the final word.
        return {"ok": True, "authenticated": False, "inconclusive": True,
                "csrf_token": refreshed_csrf or csrf_token,
                "detail": "saved; m.kuku.lu only answers real browsers (HTTP %d to "
                          "this pre-check), the background browser will verify it"
                          % listing.status_code}
    if listing.status_code >= 400:
        authenticated = False
        detail = "m.kuku.lu returned HTTP %d" % listing.status_code
    elif ("login" in lowered and "password" in lowered) or "not logged in" in lowered:
        authenticated = False
        detail = "session not accepted (looks logged out); re-copy the cookie"
    elif len(session_hash) < MIN_SESSION_LEN:
        authenticated = False
        detail = "token looks too short to be a real session cookie"

    return {"ok": True, "authenticated": authenticated, "inconclusive": False,
            "csrf_token": refreshed_csrf or csrf_token, "detail": detail}
