# Unified Mail Aggregator / Master Inbox

One local dashboard for all your Gmail, Outlook and InstAddr (m.kuku.lu)
mailboxes. Background workers pull every account into one SQLite database,
extract OTP codes and verification links, and push new mail to the browser
over a WebSocket.

## Project structure

```
mail-aggregator/
  run.py                    start the server
  start.bat                 one-click setup + start on Windows
  requirements.txt
  config.example.json       copy to config.json and edit
  accounts.example.csv      bulk import template
  app/
    main.py                 FastAPI app: REST API, WebSocket, webhook
    config.py               config.json store, account validation, CSV parsing
    database.py             SQLite schema and queries (emails.db)
    ingest.py               MIME parsing, de-duplication, live notification
    extractor.py            HTML to text, OTP and link extraction
    imap_worker.py          one thread per Gmail / Outlook / IMAP account
    oauth.py                OAuth2 refresh + XOAUTH2 for IMAP
    instaddr_browser.py     InstAddr Method B: async real-time Playwright worker
    instaddr_link.py        link payload parsing + instant session validation
    instaddr_poller.py      page parsers + legacy cookie poller
    smtp_receiver.py        InstAddr Method A: catch-all SMTP receiver
    manager.py              starts / stops / tracks workers
    events.py               thread-safe WebSocket fan-out
    static/index.html       dashboard (Tailwind via CDN + vanilla JS)
  tools/
    oauth_helper.py         get a Gmail / Outlook refresh token
    instaddr_login.py       optional local login/check helper (legacy)
    instaddr_reimport.py    re-fetch InstAddr mail after a parser change
    instaddr_debug.py       legacy cookie mode: test a session, dump pages
  tests/test_core.py        offline tests (pytest)
```

## Quick start

Windows: double-click `start.bat`. On first run it creates the virtual
environment, installs the requirements, copies `config.example.json` to
`config.json`, and starts the dashboard.

Manual (any OS):

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt        (Windows: .venv\Scripts\pip)
cp config.example.json config.json               (optional; created if missing)
.venv/bin/python run.py
```

Open http://127.0.0.1:8000 . Accounts can be added three ways:

1. Edit `config.json` (`accounts` list) and restart.
2. Dashboard -> Accounts -> "Add one account".
3. Dashboard -> Accounts -> "Bulk import": upload a CSV shaped like
   `accounts.example.csv`, or a JSON file with an `accounts` list.

Workers start immediately after an account is saved; no restart needed.
On the first sync only the newest `initial_fetch` messages per folder are
imported, after that only new mail.

Run the tests with `pip install pytest httpx` then `python -m pytest tests -q`.

## Gmail

1. Turn on 2-Step Verification, then create an App Password
   (https://myaccount.google.com/apppasswords).
2. Add the account with `platform: gmail` and the app password.
3. OTP mail is often filed as spam. Add the spam folder per account:
   `"folders": ["INBOX", "[Gmail]/Spam"]` (the folder name is localized on
   non-English accounts).

OAuth2 alternative: create a "Desktop app" OAuth client in Google Cloud,
enable the Gmail API, then run
`python tools/oauth_helper.py gmail --client-id ... --client-secret ...`
and paste the printed `oauth` block into the account entry.

## Outlook / Hotmail / Microsoft 365

Microsoft has retired plain password sign-in for IMAP on Outlook.com, so
these accounts need OAuth2:

1. Register an app at https://entra.microsoft.com (App registrations).
   Supported account types: include personal Microsoft accounts.
   Under Authentication enable "Allow public client flows".
   Add the delegated API permission `IMAP.AccessAsUser.All`.
2. For each mailbox run
   `python tools/oauth_helper.py outlook --client-id <APP_ID>`
   and sign in with that mailbox. Use `--tenant organizations` (or your
   tenant id) for work accounts.
3. Paste the printed `oauth` block into the account entry, or fill the
   `client_id`, `refresh_token`, `tenant` CSV columns.

Rotated refresh tokens are written back to `config.json` automatically.

## InstAddr (m.kuku.lu) and custom domains

InstAddr has no IMAP and no official API, so there are two routes. Use one
route per address, otherwise the same mail is stored twice.

### Method A: forward / push (most reliable)

All three variants end in the same pipeline and are labelled "InstAddr".

* A1 - Forward into a master Gmail/Outlook box. If your InstAddr plan
  offers forwarding for an address, forward it to one mailbox that is
  already configured here. List your custom domains in
  `instaddr.forward_domains`; mail addressed to those domains is then shown
  as platform InstAddr under the original address instead of the master box.
* A2 - Webhook. `POST /api/webhook/inbound?token=<webhook_secret>&to=<addr>`
  accepts a raw RFC 822 message as the request body, JSON
  (`{"from","to","subject","text","html"}` or `{"raw_base64"}`), or
  Mailgun / SendGrid inbound-parse form posts. The exact URL with your
  secret is shown in Dashboard -> Accounts.
* A3 - SMTP catch-all. Set `smtp_receiver.enabled` to true; the app then
  accepts mail for any address on port 2525 and never relays.

For a custom domain whose DNS you control, you can deliver straight to the
webhook without InstAddr in the path, for example with Cloudflare Email
Routing and this Email Worker (set WEBHOOK_URL and WEBHOOK_SECRET as Worker
variables). Note that this moves the domain's MX away from InstAddr.

```js
export default {
  async email(message, env) {
    const raw = await new Response(message.raw).arrayBuffer();
    const url = env.WEBHOOK_URL + "?token=" + env.WEBHOOK_SECRET +
                "&to=" + encodeURIComponent(message.to);
    const res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "message/rfc822" },
      body: raw,
    });
    if (!res.ok) { message.setReject("aggregator unavailable"); }
  },
};
```

The webhook and SMTP receiver must be reachable from the internet. Put the
app on a VPS or expose it with a tunnel (for example `cloudflared tunnel
--url http://127.0.0.1:8000`) and set `server.dashboard_password` first.

### Method B: real-time browser worker (Playwright, recommended)

A headless Chromium owns each InstAddr session. There is nothing to log in
to from the command line and no cookie to re-paste when tokens rotate.

Install (Windows, in the project folder, PowerShell or cmd):

```
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python -m playwright install chromium
```

Browser choice. On Windows `instaddr.browser.channel` defaults to `msedge`
(written into `config.json` on first run), because Smart App Control blocks
Playwright's bundled `chrome-headless-shell.exe` with `BrowserType.launch:
spawn UNKNOWN`. Edge ships with Windows, so `playwright install chromium` is
optional there. If the configured browser cannot start, the worker falls back
to Chrome and then the bundled Chromium by itself. On Linux/macOS the bundled
Chromium is the default (Linux also needs
`python -m playwright install-deps chromium`).

Then start the app and connect accounts from the dashboard: `python run.py`.

#### Connecting an account (zero-trust, no login in this tool)

Linking is 100% dashboard-driven; you never edit config.json and never type an
InstAddr password. The session token is validated against m.kuku.lu the moment
you link (you get instant "session is live" / "looks logged out" feedback) and
stored in emails.db, not in config.json.

Open Dashboard -> Accounts. You never type an InstAddr password here; you
hand the tool a session that is already alive in your own browser. Two ways:

* Bookmarklet (1 click). Drag the "Link InstAddr -> Master Inbox" button to
  your bookmarks bar. Open m.kuku.lu in your normal browser while logged in,
  click the bookmark, type a label. It reads the active session cookie
  (`cookie_sessionhash` / `shash`) and your User-Agent and sends them to the
  tool. (If the cookie is HttpOnly and the bookmarklet reports it cannot read
  it, use the paste option below.)
* Manual token paste. In DevTools -> Application -> Cookies -> m.kuku.lu copy
  the `cookie_sessionhash` value, paste it with a label in the "Link InstAddr"
  form. Optionally add `cookie_csrf_token` and a proxy.

Once linked, the session is validated instantly and then kept alive on its own: it reloads the
inbox page periodically so the site renews its cookies, and saves the
renewed cookies back into the `instaddr_sessions` table in emails.db. The session survives
you logging out of your own browser or shutting the PC down. If it ever stops
matching (the site invalidates it), the account turns red in Accounts with a
one-line "re-link" message instead of silently polling an empty mailbox.

#### Session lifecycle (production-grade)

* The link endpoint parses any shape of payload (JSON, form, a raw cookie
  string, or a document.cookie dump from the bookmarklet), cleans it, and can
  never 500 on bad input - a missing/garbled token returns a clean 400.
* Sessions live in the database (`instaddr_sessions`), so linking, renewing and
  dropping accounts never touch config.json.
* The worker runs a heartbeat: it reloads the inbox page every `reload_minutes`,
  writes the site's renewed cookies and storage state back to the database, and
  keeps the session alive across the site's own cookie rotation - logging out of
  your own browser or shutting the PC down does not end it.
* Every poll cycle is wrapped: an expected expiry logs a clean one-line warning
  and flips the account to "expired" (re-link to recover, picked up
  automatically); any unexpected error logs a full traceback server-side and
  shows a one-line status, but never crashes the worker thread.

#### Real-time delivery

* Each account keeps a real inbox tab open in the background browser, like
  the native web inbox. List responses the site's own page requests are
  captured as they happen, and the list endpoint is also fetched every
  `instaddr.poll_interval` seconds (default 2). New mail is pushed to the
  dashboard over the WebSocket; no page refresh. The dashboard additionally
  checks the mail count every 3 seconds in case a socket event is lost.
* Stale tokens are detected, not waited out: if the list comes back empty for
  an inbox known to hold mail, or the request fails, the tab is reloaded at
  once to get fresh tokens. The scheduled reload (`reload_minutes`, default
  5) is only the session heartbeat.
* Messages are handled newest first and independently. One message that
  cannot be opened never blocks the others; after 4 failed attempts its inbox
  row (sender, recipient, subject) is stored with a note in place of the body.
* A body served from a separate frame document is fetched too, and images
  that m.kuku.lu serves itself (which need the session cookie) are embedded
  into the stored message so they display in the dashboard.
* Each account's tab costs memory (roughly 50-150 MB). With dozens of
  accounts, watch RAM.

#### Timestamps and headers

* InstAddr shows a clock time and an age per message ("04:06 (22min)"), not a
  full timestamp. The stored time is derived from those: exact to the minute
  for mail up to a few hours old, to the day for older backlog. Mail arriving
  while the app runs is stamped within seconds of arrival.
* InstAddr exposes no raw RFC 822 headers. "Show full headers" lists
  everything its pages do show (sender, recipient, return path, message
  number, the list row, and the text around the message). Gmail, Outlook,
  IMAP, SMTP and webhook mail keeps its complete original header block.

#### When a message shows no body

Tick "Save raw InstAddr pages" under Accounts -> Troubleshooting, wait for
one new message, and look in the `debug_out` folder next to `config.json`:
`<account>-list.html` (inbox list), `<account>-view-latest.html` (the
message page) and, if used, `<account>-frame-latest.html`. Find the element
that holds the body in the view page and put its CSS selector in
`instaddr.body_selector`. These files contain your mail and session-bound
keys; treat them as private.

#### Per-account proxy (anti-ban)

Give any account its own proxy in the Accounts form, the `proxy` field in
`config.json`, or the `proxy` CSV column:

```
http://user:pass@host:port      socks5://user:pass@host:port
```

Each account already runs in its own isolated browser context (separate
cookie jar and User-Agent); the proxy puts it on its own IP too. Accounts
with no proxy use the machine's connection.

#### High volume and paging

The worker reads the first inbox page each poll. If a whole page is new since
the previous poll, older mail may have moved to page 2. Set
`instaddr.browser.page_param` (and `page_base`) to the list's paging
parameter and the worker walks back through older pages until it reaches mail
it already has. To find the parameter: open the inbox in your browser with
DevTools -> Network, go to the list's second page, and read the
`recv._ajax.php` request. Without it, the worker logs a warning when a full
page was new; lower `poll_interval`, or use Method A (push, no polling window).

#### Sender / recipient and the body

* The list and view header line `Sender <addr> >> recipient@yourdomain` is
  parsed so the address before the arrow is stored as From and the address
  after it as the real recipient (To). A `bounces+user=domain@...` VERP
  address is decoded as a fallback. The recipient is shown as a badge on
  every inbox row so you can see which of your addresses received the mail.
* The email body wrapper (`#area_data`-style id, an `iframe srcdoc`, or the
  `dir="ltr"` block) is isolated from m.kuku.lu's page, and everything inside
  it is kept exactly as sent: tables, inline CSS, forms, and elements whose
  class or id happens to be `header`, `footer`, `banner`, `btn`, `menu`...
  Only `<script>` is dropped. The dashboard shows this original HTML by
  default in a sandboxed iframe that adds no layout CSS of its own, loads
  remote images, and never runs the email's scripts. Encoding is kept as-is.
* Mail is parsed once, when fetched. Messages stored by an older version keep
  their old HTML; to fetch them again with the current parser, stop the app
  and run `python tools\instaddr_reimport.py`.
* The instant check on linking uses a plain HTTP client, which m.kuku.lu's
  bot protection answers with HTTP 403 even for valid sessions. That result
  is treated as "pending", not as a failure; the browser worker verifies the
  session and the account turns green (or shows the reason) within seconds.

If parsing ever looks wrong on a real message, set `instaddr.body_selector`
(a CSS selector for the body container) or dump the raw pages:

```
python tools\\instaddr_login.py --account <label> --check-only --dump-dir debug_out
```

#### Tuning the session guard and reload

* `session_guard` (default true) raises the re-link error when the live
  session stops matching the linked one. Set false if it misfires for you.
* `reload_minutes` (default 10) is how often the inbox page is reloaded to
  renew cookies.

Legacy cookie mode. Accounts with `"mode": "session"` and a `session_key`
still use the old synchronous `requests` poller in `app/instaddr_poller.py`.
A `session_key` with `"mode": "browser"` is migrated: the cookie seeds the
browser once, then the browser keeps it fresh.

Note: this uses undocumented pages that can change without notice, and
automated access may be restricted by the service terms - check them.

## Built-in rules (from field debugging)

These are enforced in code and covered by tests in `tests/test_core.py`:

1. Windows launches the worker with system Edge by default; other browsers
   are automatic fallbacks.
2. The plain-HTTP session pre-check treats HTTP 403/429/503 (bot protection)
   and network errors as "pending", never as failure; the browser worker
   decides. Pasted tokens are sanitized (`SHASH%3A...` becomes `SHASH:...`,
   quotes and whitespace removed) in the link form, CSV/JSON import and
   config.json alike.
3. Inside the isolated mail body (`#area_data`, `iframe[srcdoc]`,
   `dir="ltr"`) only `<script>` and `<noscript>` are removed. No keyword or
   class filter ever runs on the email's own markup.
4. Every loop that removes tags iterates over a snapshot and skips elements
   whose `attrs` is no longer a dict (already destroyed with a parent).
5. One thread per IMAP account (`ImapWorker` / `IMAPWorker`), one asyncio
   task per InstAddr browser account, legacy `InstAddrWorker` for cookie
   mode; all state (UIDs, seen messages, sessions) lives in `emails.db`.
   `tools/instaddr_reimport.py` re-fetches InstAddr mail after a parser
   change and keeps linked sessions.

## Database

`emails.db` (SQLite, WAL). Table `emails`:

`id, source_platform, account_email, custom_domain, recipient, sender,
subject, body_text, body_html, extracted_otp, extracted_link,
extracted_otp_or_link, message_id, dedupe_hash (UNIQUE), received_at,
fetched_at, is_read`

Duplicates are blocked by `dedupe_hash` = SHA-256 of the receiving address
plus the Message-ID (or a content hash when there is no Message-ID).
`received_at` is UTC ISO 8601.

## Security notes

* `config.json` and `browser_profiles/` hold passwords, tokens and login
  cookies in plain text.
  The app sets it to mode 600; keep it out of backups and git (.gitignore
  already lists it). The API never returns these secrets.
* The dashboard binds to 127.0.0.1 and has no login by default. Before
  binding to another interface or tunnelling, set
  `server.dashboard_password` (HTTP Basic, any username).
* IMAP folders are opened read-only; nothing is marked read or deleted on
  the mail servers. "Read" and "Delete" in the dashboard are local only.
* Original HTML is shown in a sandboxed iframe (no scripts). Remote images
  in that view can still load, which tells the sender the mail was opened;
  the default plain-text view loads nothing.

## Tuning

* `imap.poll_interval` (seconds, default 10). With many accounts on one IP
  keep this at 10 or higher to stay clear of provider rate limits.
* Failed logins back off exponentially up to 5 minutes; the reason is shown
  per account in Dashboard -> Accounts.
* SQLite is comfortable into the hundreds of thousands of messages. Search
  uses LIKE over sender, subject, body, account and code.
