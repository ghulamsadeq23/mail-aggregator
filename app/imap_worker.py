"""One background thread per Gmail / Outlook / generic IMAP account.

Each worker keeps a persistent IMAP connection, remembers the last
seen UID per folder in the database and fetches only newer messages.
Mailboxes are opened read-only and messages are fetched with
BODY.PEEK[], so nothing is marked as read on the server.
"""

import imaplib
import logging
import threading

from .oauth import xoauth2_string

log = logging.getLogger("imap")

SOCKET_TIMEOUT = 45
MAX_BACKOFF = 300


def quote_mailbox(name):
    if name.startswith('"') and name.endswith('"'):
        return name
    return '"%s"' % name.replace("\\", "\\\\").replace('"', '\\"')


class ImapWorker(threading.Thread):
    def __init__(self, account, store, db, ingestor, token_provider, status_cb):
        super().__init__(name="imap-%s" % account["email"], daemon=True)
        self.account = account
        self.store = store
        self.db = db
        self.ingestor = ingestor
        self.token_provider = token_provider
        self.status_cb = status_cb
        self.stop_event = threading.Event()
        self.conn = None

    # ---- lifecycle ----------------------------------------------------

    def stop(self):
        self.stop_event.set()

    def run(self):
        backoff = 5
        while not self.stop_event.is_set():
            try:
                self._run_session()
                backoff = 5
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                if isinstance(message, bytes):
                    message = message.decode("utf-8", errors="replace")
                log.warning("%s: %s (retry in %ss)", self.account["email"], message, backoff)
                self.status_cb(self.account["id"], "error", message[:300])
                self._close()
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
        self._close()
        self.status_cb(self.account["id"], "stopped", "")

    def _close(self):
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass

    # ---- IMAP ---------------------------------------------------------

    def _connect(self):
        account = self.account
        self.status_cb(account["id"], "connecting", "")
        conn = imaplib.IMAP4_SSL(account["host"], account["port"], timeout=SOCKET_TIMEOUT)
        if account["oauth"]["refresh_token"]:
            token = self.token_provider.access_token(account)
            auth = xoauth2_string(account["email"], token)
            try:
                conn.authenticate("XOAUTH2", lambda _challenge: auth.encode("utf-8"))
            except imaplib.IMAP4.error:
                self.token_provider.invalidate(account["id"])
                raise
        else:
            conn.login(account["email"], account["password"])
        log.info("%s: connected to %s", account["email"], account["host"])
        return conn

    def _run_session(self):
        self.conn = self._connect()
        folders = self.account["folders"] or self.store.get("imap", "folders", default=["INBOX"])
        while not self.stop_event.is_set():
            new_count = 0
            for folder in folders:
                new_count += self._sync_folder(self.conn, folder)
            self.status_cb(self.account["id"], "ok", "", new_count)
            interval = max(3, int(self.store.get("imap", "poll_interval", default=10)))
            if self.stop_event.wait(interval):
                break

    def _sync_folder(self, conn, folder):
        """Fetch messages newer than the stored UID. Returns count stored."""
        typ, data = conn.select(quote_mailbox(folder), readonly=True)
        if typ != "OK":
            raise RuntimeError("cannot open folder %s: %s" % (folder, data))
        _, validity_data = conn.response("UIDVALIDITY")
        uid_validity = "0"
        if validity_data and validity_data[0]:
            uid_validity = validity_data[0].decode("ascii", errors="replace")

        state_key = "imap:%s:%s" % (self.account["id"], folder)
        state = self.db.get_state(state_key, "")
        last_uid = None
        if state:
            saved_validity, _, saved_uid = state.partition(":")
            if saved_validity == uid_validity and saved_uid.isdigit():
                last_uid = int(saved_uid)

        if last_uid is None:
            # First run for this folder: import only the newest few.
            typ, data = conn.uid("SEARCH", None, "ALL")
            if typ != "OK":
                raise RuntimeError("UID SEARCH failed in %s" % folder)
            all_uids = [int(u) for u in (data[0] or b"").split()]
            initial = max(0, int(self.store.get("imap", "initial_fetch", default=20)))
            uids = all_uids[-initial:] if initial else []
            high_water = max(all_uids) if all_uids else 0
        else:
            typ, data = conn.uid("SEARCH", None, "UID", "%d:*" % (last_uid + 1))
            if typ != "OK":
                raise RuntimeError("UID SEARCH failed in %s" % folder)
            # "N:*" always returns the newest message, so filter again.
            uids = [int(u) for u in (data[0] or b"").split() if int(u) > last_uid]
            high_water = last_uid

        stored = 0
        for uid in sorted(uids):
            if self.stop_event.is_set():
                break
            typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK":
                raise RuntimeError("UID FETCH %d failed in %s" % (uid, folder))
            raw = None
            for part in data or []:
                if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                    raw = bytes(part[1])
                    break
            if raw:
                try:
                    row_id = self.ingestor.ingest_mime(
                        raw, self.account["platform"], self.account["email"])
                    if row_id is not None:
                        stored += 1
                except Exception:
                    log.exception("%s: failed to parse UID %d", self.account["email"], uid)
            high_water = max(high_water, uid)
            self.db.set_state(state_key, "%s:%d" % (uid_validity, high_water))

        self.db.set_state(state_key, "%s:%d" % (uid_validity, high_water))
        return stored


# Alias kept so both spellings import cleanly.
IMAPWorker = ImapWorker
