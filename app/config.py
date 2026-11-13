"""Configuration store: loads, validates and persists config.json.

The config file holds global settings plus the list of mail accounts.
All access goes through ConfigStore so that worker threads and the web
API never write the file at the same time.
"""

import copy
import csv
import hashlib
import io
import json
import os
import secrets
import sys
import threading
from urllib.parse import unquote

PLATFORM_LABELS = {
    "gmail": "Gmail",
    "outlook": "Outlook",
    "instaddr": "InstAddr",
    "imap": "IMAP",
}

IMAP_DEFAULTS = {
    "gmail": ("imap.gmail.com", 993),
    "outlook": ("outlook.office365.com", 993),
}

GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}
OUTLOOK_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com",
                   "outlook.jp", "hotmail.co.uk", "live.co.uk"}

# Windows Smart App Control blocks Playwright's bundled
# chrome-headless-shell.exe ("BrowserType.launch: spawn UNKNOWN"), so the
# system Edge is the default browser there. "" = Playwright's own Chromium.
DEFAULT_BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else ""

DEFAULT_CONFIG = {
    "server": {
        "host": "127.0.0.1",
        "port": 8000,
        "dashboard_password": "",
    },
    "database_path": "emails.db",
    "imap": {
        "poll_interval": 10,
        "initial_fetch": 20,
        "folders": ["INBOX"],
    },
    "instaddr": {
        "base_url": "https://m.kuku.lu",
        "poll_interval": 2,
        "initial_fetch": 20,
        "forward_domains": [],
        "list_regex": "",
        "body_selector": "",
        "debug_dump": False,
        "browser": {
            "headless": True,
            "channel": DEFAULT_BROWSER_CHANNEL,
            "profile_dir": "browser_profiles",
            "inbox_path": "/recv.php",
            "reload_minutes": 5,
            "capture_timeout": 10,
            "session_guard": True,
            "page_param": "",
            "page_base": 0,
            "max_pages": 50,
        },
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0 Safari/537.36",
    },
    "smtp_receiver": {
        "enabled": False,
        "host": "0.0.0.0",
        "port": 2525,
    },
    "webhook_secret": "",
    "link_token": "",
    "accounts": [],
}

CSV_ALIASES = {
    "app_password": "password",
    "app-password": "password",
    "app password": "password",
    "pass": "password",
    "mail": "email",
    "address": "email",
    "session": "session_key",
    "session key": "session_key",
    "instaddr session key": "session_key",
    "instaddr_session_key": "session_key",
    "sessionhash": "session_key",
    "csrf": "csrf_token",
    "proxy_url": "proxy",
    "user agent": "user_agent",
    "useragent": "user_agent",
    "imap_host": "host",
    "imap_port": "port",
}


def _deep_merge(base, override):
    """Return base updated recursively with override (dicts only)."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def detect_platform(email_addr, session_key=""):
    """Guess the platform from the address when the user left it blank."""
    domain = email_addr.rsplit("@", 1)[-1].lower() if "@" in email_addr else ""
    if session_key:
        return "instaddr"
    if domain in GMAIL_DOMAINS:
        return "gmail"
    if domain in OUTLOOK_DOMAINS:
        return "outlook"
    return "imap"


def account_id_for(platform, email_addr):
    digest = hashlib.sha1(("%s|%s" % (platform, email_addr)).encode("utf-8"))
    return digest.hexdigest()[:12]


def normalize_account(raw):
    """Validate one raw account dict and return the canonical form.

    Raises ValueError with a readable message when the entry is unusable.
    """
    if not isinstance(raw, dict):
        raise ValueError("account entry must be an object")

    email_addr = str(raw.get("email") or "").strip().lower()
    if not email_addr:
        raise ValueError("missing email")

    session_key = str(raw.get("session_key") or "").strip().strip("\"'")
    if "%" in session_key:
        # Browsers store the cookie URL-encoded ("SHASH%3A..."); the site
        # expects the decoded value ("SHASH:...").
        session_key = unquote(session_key)
    platform = str(raw.get("platform") or "").strip().lower()
    if not platform:
        platform = detect_platform(email_addr, session_key)
    if platform not in PLATFORM_LABELS:
        raise ValueError("unknown platform '%s'" % platform)

    oauth_raw = raw.get("oauth") or {}
    oauth = {
        "client_id": str(oauth_raw.get("client_id") or raw.get("client_id") or "").strip(),
        "client_secret": str(oauth_raw.get("client_secret") or raw.get("client_secret") or "").strip(),
        "refresh_token": str(oauth_raw.get("refresh_token") or raw.get("refresh_token") or "").strip(),
        "tenant": str(oauth_raw.get("tenant") or raw.get("tenant") or "").strip(),
    }

    password = str(raw.get("password") or "").strip()
    if platform == "gmail":
        # Google shows app passwords as "abcd efgh ijkl mnop".
        password = password.replace(" ", "")

    folders = raw.get("folders") or []
    if isinstance(folders, str):
        folders = [f.strip() for f in folders.replace("|", ";").split(";") if f.strip()]

    host = str(raw.get("host") or "").strip()
    port_raw = raw.get("port")
    try:
        port = int(port_raw) if port_raw not in (None, "") else 0
    except (TypeError, ValueError):
        raise ValueError("invalid port '%s'" % port_raw)

    mode = str(raw.get("mode") or "").strip().lower()
    if platform == "instaddr":
        # "browser": Playwright with a saved login (tools/instaddr_login.py).
        # "session": legacy requests poller driven by a copied cookie.
        if not mode:
            mode = "session" if session_key else "browser"
        if mode not in ("browser", "session"):
            raise ValueError("InstAddr mode must be 'browser' or 'session'")
        if mode == "session" and not session_key:
            raise ValueError("InstAddr session mode needs session_key")
    else:
        if not password and not oauth["refresh_token"]:
            raise ValueError("IMAP account needs password or oauth refresh_token")
        if "@" not in email_addr:
            raise ValueError("invalid email address")
        if platform == "imap" and not host:
            raise ValueError("generic IMAP account needs host")
        if not host:
            host = IMAP_DEFAULTS[platform][0]
        if not port:
            port = IMAP_DEFAULTS.get(platform, ("", 993))[1]

    enabled = raw.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "no", "off")

    return {
        "id": str(raw.get("id") or account_id_for(platform, email_addr)),
        "platform": platform,
        "email": email_addr,
        "password": password,
        "host": host,
        "port": port,
        "folders": list(folders),
        "oauth": oauth,
        "session_key": session_key,
        "mode": mode if platform == "instaddr" else "",
        "csrf_token": str(raw.get("csrf_token") or "").strip(),
        "proxy": str(raw.get("proxy") or "").strip(),
        "user_agent": str(raw.get("user_agent") or "").strip(),
        "base_url": str(raw.get("base_url") or "").strip(),
        "enabled": bool(enabled),
    }


def parse_accounts_csv(text):
    """Parse CSV text into (accounts, errors).

    Expected header columns (order does not matter, extra ones ignored):
    platform,email,password,session_key,csrf_token,host,port,folders,
    client_id,client_secret,refresh_token,tenant
    """
    text = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    accounts = []
    errors = []
    if not reader.fieldnames:
        return accounts, ["CSV file has no header row"]
    for line_no, row in enumerate(reader, start=2):
        cleaned = {}
        for key, value in row.items():
            if key is None:
                continue
            name = key.strip().lower()
            name = CSV_ALIASES.get(name, name)
            cleaned[name] = (value or "").strip()
        if not any(cleaned.values()):
            continue
        try:
            accounts.append(normalize_account(cleaned))
        except ValueError as exc:
            errors.append("line %d: %s" % (line_no, exc))
    return accounts, errors


def public_account(account):
    """Account view that is safe to send to the browser (no secrets)."""
    if account["platform"] == "instaddr":
        auth = "browser profile" if account.get("mode") == "browser" else "session"
    elif account["oauth"]["refresh_token"]:
        auth = "oauth2"
    else:
        auth = "password"
    return {
        "id": account["id"],
        "platform": account["platform"],
        "platform_label": PLATFORM_LABELS[account["platform"]],
        "email": account["email"],
        "auth": auth,
        "mode": account.get("mode", ""),
        "host": account["host"],
        "folders": account["folders"],
        "proxy_set": bool(account.get("proxy")),
        "enabled": account["enabled"],
    }


class ConfigStore:
    """Thread-safe owner of config.json."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULT_CONFIG)
        self.load_errors = []
        self._load()

    def _load(self):
        raw = {}
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        data = _deep_merge(DEFAULT_CONFIG, raw)

        accounts = []
        seen = set()
        for index, entry in enumerate(raw.get("accounts", [])):
            try:
                account = normalize_account(entry)
            except ValueError as exc:
                self.load_errors.append("accounts[%d]: %s" % (index, exc))
                continue
            if account["id"] in seen:
                continue
            seen.add(account["id"])
            accounts.append(account)
        data["accounts"] = accounts

        changed = not os.path.exists(self.path)
        if not data.get("webhook_secret"):
            data["webhook_secret"] = secrets.token_urlsafe(24)
            changed = True
        if not data.get("link_token"):
            data["link_token"] = secrets.token_urlsafe(18)
            changed = True
        self._data = data
        if changed:
            self.save()

    def save(self):
        with self._lock:
            tmp_path = self.path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2)
            os.replace(tmp_path, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def get(self, *keys, default=None):
        """Read a nested setting, e.g. get("imap", "poll_interval").

        A missing key OR an explicit null in config.json both yield the
        default. Returning None for a key the user wrote as null was a
        footgun: callers do `settings.get(...)` / iterate the result and
        crashed with 'NoneType' errors. Null now means "use the default".
        """
        with self._lock:
            node = self._data
            for key in keys:
                if not isinstance(node, dict) or key not in node:
                    return default
                node = node[key]
            if node is None:
                return default
            return copy.deepcopy(node)

    def set_value(self, keys, value):
        """Set one nested setting and save, e.g. set_value(("instaddr", "debug_dump"), True)."""
        with self._lock:
            node = self._data
            for key in keys[:-1]:
                if not isinstance(node.get(key), dict):
                    node[key] = {}
                node = node[key]
            node[keys[-1]] = value
            self.save()

    def accounts(self):
        with self._lock:
            return copy.deepcopy(self._data["accounts"])

    def add_accounts(self, new_accounts):
        """Insert or replace accounts. Returns (added, updated)."""
        added = 0
        updated = 0
        with self._lock:
            by_id = {acc["id"]: i for i, acc in enumerate(self._data["accounts"])}
            for account in new_accounts:
                if account["id"] in by_id:
                    self._data["accounts"][by_id[account["id"]]] = account
                    updated += 1
                else:
                    by_id[account["id"]] = len(self._data["accounts"])
                    self._data["accounts"].append(account)
                    added += 1
            self.save()
        return added, updated

    def remove_account(self, account_id):
        with self._lock:
            before = len(self._data["accounts"])
            self._data["accounts"] = [a for a in self._data["accounts"]
                                      if a["id"] != account_id]
            removed = len(self._data["accounts"]) != before
            if removed:
                self.save()
            return removed

    def link_instaddr_session(self, account_id, session_key="", csrf_token="",
                              user_agent="", proxy=None):
        """Attach a pasted/bookmarklet session to an InstAddr account.

        Creates the account on first link (mode "browser"). Returns the
        canonical account dict, or None if the id is unknown and no email
        is derivable.
        """
        with self._lock:
            for account in self._data["accounts"]:
                if account["id"] == account_id:
                    if session_key:
                        account["session_key"] = session_key
                    if csrf_token:
                        account["csrf_token"] = csrf_token
                    if user_agent:
                        account["user_agent"] = user_agent
                    if proxy is not None:
                        account["proxy"] = proxy
                    account["mode"] = "browser"
                    account["enabled"] = True
                    self.save()
                    return copy.deepcopy(account)
        return None

    def update_refresh_token(self, account_id, refresh_token):
        """Persist a rotated OAuth refresh token."""
        with self._lock:
            for account in self._data["accounts"]:
                if account["id"] == account_id:
                    if account["oauth"]["refresh_token"] != refresh_token:
                        account["oauth"]["refresh_token"] = refresh_token
                        self.save()
                    return
