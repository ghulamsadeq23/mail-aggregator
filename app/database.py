"""SQLite storage for the unified inbox.

One connection is shared by all threads and guarded by a lock. WAL mode
keeps dashboard reads fast while workers insert.
"""

import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_platform TEXT NOT NULL,
    account_email TEXT NOT NULL,
    custom_domain TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body_text TEXT NOT NULL DEFAULT '',
    body_html TEXT NOT NULL DEFAULT '',
    extracted_otp TEXT NOT NULL DEFAULT '',
    extracted_link TEXT NOT NULL DEFAULT '',
    extracted_otp_or_link TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    raw_headers TEXT NOT NULL DEFAULT '',
    dedupe_hash TEXT NOT NULL UNIQUE,
    received_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_emails_received ON emails (received_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_emails_platform ON emails (source_platform);
CREATE INDEX IF NOT EXISTS idx_emails_account ON emails (account_email);
CREATE INDEX IF NOT EXISTS idx_emails_domain ON emails (custom_domain);
CREATE INDEX IF NOT EXISTS idx_emails_unread ON emails (is_read);

CREATE TABLE IF NOT EXISTS instaddr_seen (
    account_id TEXT NOT NULL,
    num TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (account_id, num)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Volatile InstAddr sessions live here, not in config.json: a linked
-- session is just a cookie_sessionhash mapped to temporary storage, so
-- the dashboard can add/renew/drop them without editing config.
CREATE TABLE IF NOT EXISTS instaddr_sessions (
    account_id TEXT PRIMARY KEY,
    email TEXT NOT NULL DEFAULT '',
    session_hash TEXT NOT NULL DEFAULT '',
    csrf_token TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    proxy TEXT NOT NULL DEFAULT '',
    storage_state TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'linked',
    detail TEXT NOT NULL DEFAULT '',
    linked_at TEXT NOT NULL DEFAULT '',
    validated_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""

SUMMARY_COLUMNS = (
    "id, source_platform, account_email, custom_domain, recipient, sender, "
    "subject, substr(body_text, 1, 200) AS snippet, extracted_otp, "
    "extracted_link, extracted_otp_or_link, received_at, fetched_at, is_read"
)

INSERT_COLUMNS = (
    "source_platform", "account_email", "custom_domain", "recipient",
    "sender", "subject", "body_text", "body_html", "extracted_otp",
    "extracted_link", "extracted_otp_or_link", "message_id", "raw_headers",
    "dedupe_hash",
    "received_at", "fetched_at", "is_read",
)


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            # Databases created by older versions lack newer columns.
            columns = {row["name"] for row in
                       self._conn.execute("PRAGMA table_info(emails)").fetchall()}
            if "raw_headers" not in columns:
                self._conn.execute(
                    "ALTER TABLE emails ADD COLUMN raw_headers TEXT NOT NULL DEFAULT ''")
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ---- emails -------------------------------------------------------

    def insert_email(self, record):
        """Insert one email. Returns the new row id, or None on duplicate."""
        values = [record.get(col, "") for col in INSERT_COLUMNS]
        placeholders = ", ".join("?" for _ in INSERT_COLUMNS)
        sql = "INSERT OR IGNORE INTO emails (%s) VALUES (%s)" % (
            ", ".join(INSERT_COLUMNS), placeholders)
        with self._lock:
            cursor = self._conn.execute(sql, values)
            self._conn.commit()
            if cursor.rowcount != 1:
                return None
            return cursor.lastrowid

    @staticmethod
    def _build_filters(platform, domain, account, query, unread_only):
        clauses = []
        params = []
        if platform:
            clauses.append("source_platform = ?")
            params.append(platform)
        if domain:
            clauses.append("custom_domain = ?")
            params.append(domain)
        if account:
            clauses.append("account_email = ?")
            params.append(account)
        if unread_only:
            clauses.append("is_read = 0")
        if query:
            for term in query.split():
                like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                clauses.append(
                    "(sender LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\' "
                    "OR body_text LIKE ? ESCAPE '\\' OR account_email LIKE ? ESCAPE '\\' "
                    "OR recipient LIKE ? ESCAPE '\\' OR extracted_otp LIKE ? ESCAPE '\\')")
                params.extend([like] * 6)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def query_emails(self, platform="", domain="", account="", query="",
                     unread_only=False, limit=50, offset=0):
        where, params = self._build_filters(platform, domain, account, query, unread_only)
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM emails" + where, params).fetchone()[0]
            rows = self._conn.execute(
                "SELECT " + SUMMARY_COLUMNS + " FROM emails" + where +
                " ORDER BY received_at DESC, id DESC LIMIT ? OFFSET ?",
                params + [int(limit), int(offset)]).fetchall()
        return total, [dict(row) for row in rows]

    def get_email(self, email_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
        return dict(row) if row else None

    def get_summary(self, email_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT " + SUMMARY_COLUMNS + " FROM emails WHERE id = ?",
                (email_id,)).fetchone()
        return dict(row) if row else None

    def set_read(self, email_id, is_read):
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE emails SET is_read = ? WHERE id = ?",
                (1 if is_read else 0, email_id))
            self._conn.commit()
            return cursor.rowcount == 1

    def mark_all_read(self, platform="", domain="", account=""):
        where, params = self._build_filters(platform, domain, account, "", False)
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE emails SET is_read = 1" + where, params)
            self._conn.commit()
            return cursor.rowcount

    def delete_email(self, email_id):
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM emails WHERE id = ?", (email_id,))
            self._conn.commit()
            return cursor.rowcount == 1

    def facets(self):
        """Counts per platform, domain and account for the sidebar."""
        result = {}
        with self._lock:
            for name, column in (("platforms", "source_platform"),
                                 ("domains", "custom_domain"),
                                 ("accounts", "account_email")):
                rows = self._conn.execute(
                    "SELECT %s AS value, COUNT(*) AS total, "
                    "SUM(CASE WHEN is_read = 0 THEN 1 ELSE 0 END) AS unread "
                    "FROM emails WHERE %s != '' GROUP BY %s "
                    "ORDER BY total DESC" % (column, column, column)).fetchall()
                result[name] = [dict(row) for row in rows]
            row = self._conn.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(CASE WHEN is_read = 0 THEN 1 ELSE 0 END), 0) AS unread "
                "FROM emails").fetchone()
            result["total"] = row["total"]
            result["unread"] = row["unread"]
        return result

    # ---- sync state ---------------------------------------------------

    def get_state(self, key, default=None):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key, value):
        with self._lock:
            self._conn.execute(
                "INSERT INTO sync_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)))
            self._conn.commit()

    # ---- InstAddr message tracking ------------------------------------

    def instaddr_unseen(self, account_id, nums):
        """Return the subset of nums not yet recorded for this account."""
        nums = [str(n) for n in nums]
        known = set()
        with self._lock:
            for start in range(0, len(nums), 400):
                chunk = nums[start:start + 400]
                marks = ", ".join("?" for _ in chunk)
                rows = self._conn.execute(
                    "SELECT num FROM instaddr_seen WHERE account_id = ? "
                    "AND num IN (%s)" % marks, [account_id] + chunk).fetchall()
                known.update(row["num"] for row in rows)
        return set(nums) - known

    def instaddr_mark_seen(self, account_id, nums):
        now = utc_now_iso()
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO instaddr_seen (account_id, num, seen_at) "
                "VALUES (?, ?, ?)", [(account_id, str(n), now) for n in nums])
            self._conn.commit()

    # ---- InstAddr session store ---------------------------------------

    def upsert_instaddr_session(self, account_id, email="", session_hash="",
                                csrf_token="", user_agent="", proxy=""):
        """Create or update a linked session. Only non-empty fields overwrite
        existing values, so a re-link that supplies just a new token keeps the
        stored storage_state, proxy and user agent."""
        now = utc_now_iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT account_id FROM instaddr_sessions WHERE account_id = ?",
                (account_id,)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO instaddr_sessions (account_id, email, session_hash, "
                    "csrf_token, user_agent, proxy, status, detail, linked_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'linked', '', ?, ?)",
                    (account_id, email, session_hash, csrf_token, user_agent, proxy, now, now))
            else:
                sets = ["updated_at = ?", "status = 'linked'", "detail = ''"]
                params = [now]
                for column, value in (("email", email), ("session_hash", session_hash),
                                      ("csrf_token", csrf_token), ("user_agent", user_agent),
                                      ("proxy", proxy)):
                    if value:
                        sets.append("%s = ?" % column)
                        params.append(value)
                params.append(account_id)
                self._conn.execute(
                    "UPDATE instaddr_sessions SET %s WHERE account_id = ?" % ", ".join(sets),
                    params)
            self._conn.commit()
        return self.get_instaddr_session(account_id)

    def get_instaddr_session(self, account_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM instaddr_sessions WHERE account_id = ?",
                (account_id,)).fetchone()
        return dict(row) if row else None

    def list_instaddr_sessions(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT account_id, email, status, detail, proxy, validated_at, "
                "updated_at, length(storage_state) AS state_len FROM instaddr_sessions"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_instaddr_runtime(self, account_id, storage_state=None, csrf_token=None,
                                session_hash=None, status=None, detail=None, validated=False):
        """Persist whatever the worker refreshed. Fields left None are untouched."""
        now = utc_now_iso()
        sets = ["updated_at = ?"]
        params = [now]
        for column, value in (("storage_state", storage_state), ("csrf_token", csrf_token),
                              ("session_hash", session_hash), ("status", status),
                              ("detail", detail)):
            if value is not None:
                sets.append("%s = ?" % column)
                params.append(value)
        if validated:
            sets.append("validated_at = ?")
            params.append(now)
        params.append(account_id)
        with self._lock:
            self._conn.execute(
                "UPDATE instaddr_sessions SET %s WHERE account_id = ?" % ", ".join(sets),
                params)
            self._conn.commit()

    def delete_instaddr_session(self, account_id):
        with self._lock:
            self._conn.execute(
                "DELETE FROM instaddr_sessions WHERE account_id = ?", (account_id,))
            self._conn.commit()

    def instaddr_seen_count(self, account_id):
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM instaddr_seen WHERE account_id = ?",
                (account_id,)).fetchone()[0]
