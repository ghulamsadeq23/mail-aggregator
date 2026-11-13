"""Single entry point that turns raw mail into database rows.

Every source (IMAP worker, InstAddr poller, SMTP receiver, webhook)
calls Ingestor so parsing, extraction, de-duplication and live
notification behave the same everywhere.
"""

import email
import email.policy
import email.utils
import hashlib
import logging
from datetime import timezone

from .config import PLATFORM_LABELS
from .database import utc_now_iso
from .extractor import domain_of, extract_link, extract_otp, html_to_text

log = logging.getLogger("ingest")


def _decode_part(part):
    """Best-effort decode of one MIME part to str."""
    try:
        content = part.get_content()
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        return str(content)
    except Exception:
        payload = part.get_payload(decode=True) or b""
        for charset in (part.get_content_charset(), "utf-8", "latin-1"):
            if not charset:
                continue
            try:
                return payload.decode(charset, errors="replace")
            except LookupError:
                continue
        return ""


def _header_addresses(msg, name):
    values = [str(v) for v in msg.get_all(name, [])]
    return [addr.lower() for _, addr in email.utils.getaddresses(values) if "@" in addr]


def parse_mime(raw_bytes):
    """Parse RFC 822 bytes into a plain dict of the fields we store."""
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)

    body_text = ""
    body_html = ""
    try:
        plain_part = msg.get_body(preferencelist=("plain",))
        html_part = msg.get_body(preferencelist=("html",))
    except Exception:
        plain_part = html_part = None
    if plain_part is not None:
        body_text = _decode_part(plain_part)
    if html_part is not None:
        body_html = _decode_part(html_part)
    if plain_part is None and html_part is None and not msg.is_multipart():
        body_text = _decode_part(msg)

    received_at = ""
    date_header = msg.get("Date")
    if date_header:
        try:
            parsed = email.utils.parsedate_to_datetime(str(date_header))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            received_at = parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            received_at = ""

    to_addresses = _header_addresses(msg, "To") + _header_addresses(msg, "Cc")
    delivered = (_header_addresses(msg, "X-Original-To")
                 + _header_addresses(msg, "Delivered-To")
                 + _header_addresses(msg, "X-Forwarded-To"))

    # Full original header block, exactly as received.
    header_end = len(raw_bytes)
    for marker in (b"\r\n\r\n", b"\n\n"):
        position = raw_bytes.find(marker)
        if position != -1:
            header_end = min(header_end, position)
    raw_headers = raw_bytes[:header_end].decode("utf-8", errors="replace")

    return {
        "raw_headers": raw_headers,
        "sender": str(msg.get("From", "")).strip(),
        "subject": str(msg.get("Subject", "")).strip(),
        "message_id": str(msg.get("Message-ID", "")).strip(),
        "to_addresses": to_addresses,
        "delivered_addresses": delivered,
        "body_text": body_text,
        "body_html": body_html,
        "received_at": received_at,
    }


class Ingestor:
    def __init__(self, db, hub, store):
        self.db = db
        self.hub = hub
        self.store = store

    def ingest_mime(self, raw_bytes, platform, account_email, envelope_to=""):
        """Store one raw RFC 822 message. Returns row id or None if duplicate."""
        parsed = parse_mime(raw_bytes)
        account_email = (account_email or "").lower()

        # Which address did this mail actually go to? Prefer the mailbox
        # itself when it is listed, otherwise the envelope / first To.
        recipient = (envelope_to or "").lower()
        if not recipient:
            if account_email in parsed["to_addresses"] or not parsed["to_addresses"]:
                recipient = account_email
            else:
                recipient = parsed["to_addresses"][0]

        return self.ingest_fields(
            platform=platform,
            account_email=account_email or recipient,
            recipient=recipient,
            sender=parsed["sender"],
            subject=parsed["subject"],
            body_text=parsed["body_text"],
            body_html=parsed["body_html"],
            message_id=parsed["message_id"],
            received_at=parsed["received_at"],
            raw_headers=parsed["raw_headers"],
        )

    def ingest_fields(self, platform, account_email, sender, subject,
                      body_text="", body_html="", recipient="",
                      message_id="", received_at="", dedupe_key="",
                      raw_headers=""):
        """Store one message from already-separated fields."""
        account_email = (account_email or "").strip().lower()
        recipient = (recipient or account_email).strip().lower()
        body_text = body_text or ""
        body_html = body_html or ""
        if not body_text.strip() and body_html:
            body_text = html_to_text(body_html)

        platform_label = PLATFORM_LABELS.get(str(platform).lower(), str(platform))
        custom_domain = domain_of(recipient) or domain_of(account_email)

        # Mail forwarded from InstAddr into a Gmail/Outlook/SMTP inbox is
        # re-labelled so the dashboard filters still work per platform.
        forward_domains = [d.lower() for d in
                           self.store.get("instaddr", "forward_domains", default=[])]
        if custom_domain and custom_domain in forward_domains:
            platform_label = PLATFORM_LABELS["instaddr"]
            account_email = recipient

        otp = extract_otp(subject, body_text)
        link = extract_link(body_text, body_html)

        fetched_at = utc_now_iso()
        if not received_at:
            received_at = fetched_at
        # Guard against senders with clocks in the future.
        if received_at > fetched_at:
            received_at = fetched_at

        if not dedupe_key:
            if message_id:
                dedupe_key = message_id
            else:
                dedupe_key = "|".join([sender, subject, received_at, body_text[:500]])
        dedupe_hash = hashlib.sha256(
            (account_email + "|" + dedupe_key).encode("utf-8", errors="replace")
        ).hexdigest()

        record = {
            "source_platform": platform_label,
            "account_email": account_email,
            "custom_domain": custom_domain,
            "recipient": recipient,
            "sender": sender or "",
            "subject": subject or "(no subject)",
            "body_text": body_text,
            "body_html": body_html,
            "extracted_otp": otp,
            "extracted_link": link,
            "extracted_otp_or_link": otp or link,
            "message_id": message_id or "",
            "raw_headers": raw_headers or "",
            "dedupe_hash": dedupe_hash,
            "received_at": received_at,
            "fetched_at": fetched_at,
            "is_read": 0,
        }
        row_id = self.db.insert_email(record)
        if row_id is None:
            return None

        log.info("new mail [%s] %s <- %s | %s%s", platform_label, account_email,
                 sender, record["subject"][:60], (" | OTP " + otp) if otp else "")
        summary = self.db.get_summary(row_id)
        self.hub.publish({"type": "new_email", "email": summary})
        return row_id

