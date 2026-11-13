"""Obtain an OAuth2 refresh token for one Gmail or Outlook account.

Outlook (device code flow, works for personal and work accounts):
    python tools/oauth_helper.py outlook --client-id <APP_ID> [--tenant consumers]

Gmail (loopback redirect flow, needs a "Desktop app" OAuth client):
    python tools/oauth_helper.py gmail --client-id <ID> --client-secret <SECRET>

The script prints a JSON snippet to paste into the account entry in
config.json (or the matching CSV columns).
"""

import argparse
import http.server
import json
import sys
import time
import urllib.parse
import webbrowser

import requests

MS_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
GOOGLE_SCOPE = "https://mail.google.com/"


def outlook_flow(client_id, tenant):
    base = "https://login.microsoftonline.com/%s/oauth2/v2.0" % tenant
    start = requests.post(base + "/devicecode",
                          data={"client_id": client_id, "scope": MS_SCOPE},
                          timeout=30).json()
    if "device_code" not in start:
        sys.exit("Device code request failed: %s" % json.dumps(start))
    print(start.get("message") or
          "Open %s and enter code %s" % (start["verification_uri"], start["user_code"]))
    interval = int(start.get("interval", 5))
    deadline = time.time() + int(start.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        token = requests.post(base + "/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id,
            "device_code": start["device_code"],
        }, timeout=30).json()
        if "refresh_token" in token:
            return {"client_id": client_id, "client_secret": "",
                    "refresh_token": token["refresh_token"], "tenant": tenant}
        if token.get("error") not in ("authorization_pending", "slow_down"):
            sys.exit("Token request failed: %s" % json.dumps(token))
    sys.exit("Timed out waiting for sign-in.")


class _CodeHandler(http.server.BaseHTTPRequestHandler):
    code = None

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CodeHandler.code = (query.get("code") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Done. You can close this tab and return to the terminal.")

    def log_message(self, *_args):
        return


def gmail_flow(client_id, client_secret, port):
    redirect_uri = "http://127.0.0.1:%d/" % port
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    })
    server = http.server.HTTPServer(("127.0.0.1", port), _CodeHandler)
    print("Open this URL and sign in:\n\n%s\n" % url)
    webbrowser.open(url)
    while _CodeHandler.code is None:
        server.handle_request()
    server.server_close()
    token = requests.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "authorization_code",
        "code": _CodeHandler.code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }, timeout=30).json()
    if "refresh_token" not in token:
        sys.exit("Token request failed: %s" % json.dumps(token))
    return {"client_id": client_id, "client_secret": client_secret,
            "refresh_token": token["refresh_token"], "tenant": ""}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("provider", choices=["outlook", "gmail"])
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", default="")
    parser.add_argument("--tenant", default="consumers",
                        help="Outlook only: consumers, organizations, common or a tenant id")
    parser.add_argument("--port", type=int, default=8765, help="Gmail only: loopback port")
    args = parser.parse_args()

    if args.provider == "outlook":
        oauth = outlook_flow(args.client_id, args.tenant)
    else:
        if not args.client_secret:
            sys.exit("Gmail needs --client-secret")
        oauth = gmail_flow(args.client_id, args.client_secret, args.port)

    print("\nAdd this to the account entry in config.json:\n")
    print(json.dumps({"oauth": oauth}, indent=2))


if __name__ == "__main__":
    main()
