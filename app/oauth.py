"""OAuth2 access tokens for IMAP XOAUTH2 (Gmail and Outlook).

Only the refresh-token grant is implemented here; use
tools/oauth_helper.py once per account to obtain the refresh token.
"""

import threading
import time

import requests

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/%s/oauth2/v2.0/token"
MICROSOFT_IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
GOOGLE_IMAP_SCOPE = "https://mail.google.com/"


class TokenProvider:
    """Caches access tokens per account and refreshes them when needed."""

    def __init__(self, store):
        self.store = store
        self._cache = {}
        self._lock = threading.Lock()

    def access_token(self, account):
        with self._lock:
            cached = self._cache.get(account["id"])
            if cached and cached[1] - 120 > time.time():
                return cached[0]

        oauth = account["oauth"]
        if account["platform"] == "gmail":
            url = GOOGLE_TOKEN_URL
            data = {
                "grant_type": "refresh_token",
                "client_id": oauth["client_id"],
                "client_secret": oauth["client_secret"],
                "refresh_token": oauth["refresh_token"],
            }
        else:
            url = MICROSOFT_TOKEN_URL % (oauth["tenant"] or "common")
            data = {
                "grant_type": "refresh_token",
                "client_id": oauth["client_id"],
                "refresh_token": oauth["refresh_token"],
                "scope": MICROSOFT_IMAP_SCOPE,
            }
            if oauth["client_secret"]:
                data["client_secret"] = oauth["client_secret"]

        response = requests.post(url, data=data, timeout=30)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code != 200 or "access_token" not in payload:
            detail = payload.get("error_description") or payload.get("error") or response.text[:200]
            raise RuntimeError("OAuth token refresh failed: %s" % detail)

        token = payload["access_token"]
        expires_at = time.time() + int(payload.get("expires_in", 3600))
        with self._lock:
            self._cache[account["id"]] = (token, expires_at)

        # Microsoft rotates refresh tokens; keep the newest one on disk.
        new_refresh = payload.get("refresh_token")
        if new_refresh and new_refresh != oauth["refresh_token"]:
            account["oauth"]["refresh_token"] = new_refresh
            self.store.update_refresh_token(account["id"], new_refresh)
        return token

    def invalidate(self, account_id):
        with self._lock:
            self._cache.pop(account_id, None)


def xoauth2_string(user, access_token):
    return "user=%s\x01auth=Bearer %s\x01\x01" % (user, access_token)
