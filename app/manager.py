"""Starts, stops and tracks one worker thread per configured account."""

import json
import logging
import threading

from .database import utc_now_iso
from .imap_worker import ImapWorker
from .instaddr_browser import InstAddrBrowserService
from .instaddr_poller import InstAddrWorker
from .oauth import TokenProvider

log = logging.getLogger("manager")


class WorkerManager:
    def __init__(self, store, db, ingestor, hub):
        self.store = store
        self.db = db
        self.ingestor = ingestor
        self.hub = hub
        self.token_provider = TokenProvider(store)
        self._workers = {}
        self._browser_service = None
        self._fingerprints = {}
        self._status = {}
        self._lock = threading.RLock()

    @staticmethod
    def _fingerprint(account):
        clone = json.loads(json.dumps(account))
        # A rotated refresh token must not restart the worker.
        clone.get("oauth", {}).pop("refresh_token", None)
        return json.dumps(clone, sort_keys=True)

    def _set_status(self, account_id, state, detail="", new_count=0):
        with self._lock:
            previous = self._status.get(account_id, {})
            changed = previous.get("state") != state or previous.get("detail") != detail
            self._status[account_id] = {
                "state": state,
                "detail": detail,
                "last_check": utc_now_iso(),
                "fetched": previous.get("fetched", 0) + int(new_count or 0),
            }
        if changed:
            self.hub.publish({"type": "account_status", "account_id": account_id,
                              "state": state, "detail": detail})

    def _make_worker(self, account):
        if account["platform"] == "instaddr":
            return InstAddrWorker(account, self.store, self.db, self.ingestor,
                                  self._set_status)
        return ImapWorker(account, self.store, self.db, self.ingestor,
                          self.token_provider, self._set_status)

    def reload(self):
        """Bring running workers in line with the current config."""
        with self._lock:
            enabled = [a for a in self.store.accounts() if a["enabled"]]
            browser_accounts = [a for a in enabled if a["platform"] == "instaddr"
                                and a.get("mode") == "browser"]
            browser_ids = {a["id"] for a in browser_accounts}
            wanted = {a["id"]: a for a in enabled if a["id"] not in browser_ids}

            # All browser-mode InstAddr accounts share one Playwright thread.
            if browser_accounts and self._browser_service is None:
                self._browser_service = InstAddrBrowserService(
                    self.store, self.db, self.ingestor, self._set_status)
                self._browser_service.start()
            if self._browser_service is not None:
                self._browser_service.set_accounts(browser_accounts)
            for account in browser_accounts:
                self._status.setdefault(account["id"], {
                    "state": "connecting", "detail": "",
                    "last_check": utc_now_iso(), "fetched": 0})
            for account_id in list(self._status):
                if account_id not in wanted and account_id not in browser_ids:
                    self._status.pop(account_id, None)

            for account_id in list(self._workers):
                account = wanted.get(account_id)
                if account is None or self._fingerprint(account) != self._fingerprints[account_id]:
                    self._workers.pop(account_id).stop()
                    self._fingerprints.pop(account_id, None)
                    if account is None:
                        self._status.pop(account_id, None)

            for account_id, account in wanted.items():
                if account_id in self._workers:
                    continue
                worker = self._make_worker(account)
                self._workers[account_id] = worker
                self._fingerprints[account_id] = self._fingerprint(account)
                self._status[account_id] = {"state": "connecting", "detail": "",
                                            "last_check": utc_now_iso(), "fetched": 0}
                worker.start()
            log.info("%d account worker(s) running, %d InstAddr browser account(s)",
                     len(self._workers), len(browser_accounts))

    def stop_all(self):
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
            self._fingerprints.clear()
            if self._browser_service is not None:
                workers.append(self._browser_service)
                self._browser_service = None
        for worker in workers:
            worker.stop()
        for worker in workers:
            worker.join(timeout=8)

    def statuses(self):
        with self._lock:
            return {key: dict(value) for key, value in self._status.items()}
