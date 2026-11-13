"""InstAddr Method A (part 1): catch-all SMTP receiver.

Accepts mail for any recipient and stores it. Point a forwarding rule
or an MX record at the machine running this (see README). It never
relays mail anywhere, so it cannot be abused as an open relay.
"""

import logging

from aiosmtpd.controller import Controller

from .extractor import domain_of

log = logging.getLogger("smtp")

MAX_MESSAGE_BYTES = 25 * 1024 * 1024


class CatchAllHandler:
    def __init__(self, ingestor, store):
        self.ingestor = ingestor
        self.store = store

    async def handle_DATA(self, server, session, envelope):
        raw = envelope.original_content or envelope.content
        if isinstance(raw, str):
            raw = raw.encode("utf-8", errors="replace")
        forward_domains = [d.lower() for d in
                           self.store.get("instaddr", "forward_domains", default=[])]
        for recipient in envelope.rcpt_tos:
            recipient = recipient.lower()
            platform = "instaddr" if (not forward_domains or
                                      domain_of(recipient) in forward_domains) else "imap"
            try:
                self.ingestor.ingest_mime(raw, platform, recipient, envelope_to=recipient)
            except Exception:
                log.exception("failed to store SMTP message for %s", recipient)
                return "451 Temporary local error"
        return "250 Message accepted"


class SmtpReceiver:
    def __init__(self, ingestor, store):
        self.ingestor = ingestor
        self.store = store
        self.controller = None

    def start(self):
        settings = self.store.get("smtp_receiver", default={})
        if not settings.get("enabled"):
            return False
        self.controller = Controller(
            CatchAllHandler(self.ingestor, self.store),
            hostname=settings.get("host", "0.0.0.0"),
            port=int(settings.get("port", 2525)),
            data_size_limit=MAX_MESSAGE_BYTES)
        self.controller.start()
        log.info("SMTP receiver listening on %s:%s",
                 settings.get("host", "0.0.0.0"), settings.get("port", 2525))
        return True

    def stop(self):
        if self.controller is not None:
            self.controller.stop()
            self.controller = None
