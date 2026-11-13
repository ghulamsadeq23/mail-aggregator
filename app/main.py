"""FastAPI application: REST API, live WebSocket feed, webhook, dashboard."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.websockets import WebSocketDisconnect

from .config import (ConfigStore, PLATFORM_LABELS, account_id_for,
                     normalize_account, parse_accounts_csv, public_account)
from .database import Database
from .events import EventHub
from .ingest import Ingestor
from .instaddr_link import parse_link_payload, validate_session
from .manager import WorkerManager
from .smtp_receiver import SmtpReceiver

log = logging.getLogger("web")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "static", "index.html")
CONFIG_PATH = os.environ.get("MAILAGG_CONFIG", "config.json")
MAX_WEBHOOK_BYTES = 25 * 1024 * 1024
SESSION_COOKIE = "mailagg_session"


class AppState:
    store = None
    db = None
    hub = None
    ingestor = None
    manager = None
    smtp = None


state = AppState()


@asynccontextmanager
async def lifespan(_app):
    state.store = ConfigStore(CONFIG_PATH)
    for error in state.store.load_errors:
        log.warning("config: skipped %s", error)
    db_path = state.store.get("database_path", default="emails.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(state.store.path), db_path)
    state.db = Database(db_path)
    state.hub = EventHub()
    state.hub.bind_loop(asyncio.get_running_loop())
    state.ingestor = Ingestor(state.db, state.hub, state.store)
    state.manager = WorkerManager(state.store, state.db, state.ingestor, state.hub)
    state.manager.reload()
    state.smtp = SmtpReceiver(state.ingestor, state.store)
    try:
        state.smtp.start()
    except Exception as exc:
        log.error("SMTP receiver failed to start: %s", exc)
    log.info("database: %s", db_path)
    yield
    state.smtp.stop()
    state.manager.stop_all()
    state.db.close()


app = FastAPI(title="Unified Mail Aggregator", lifespan=lifespan)


# ---- optional dashboard password ------------------------------------

def _session_token():
    password = state.store.get("server", "dashboard_password", default="")
    secret = state.store.get("webhook_secret", default="")
    return hashlib.sha256(("%s|%s" % (password, secret)).encode("utf-8")).hexdigest()


def _password_required():
    return bool(state.store.get("server", "dashboard_password", default=""))


@app.middleware("http")
async def dashboard_auth(request: Request, call_next):
    # The webhook and the zero-trust link endpoint carry their own token,
    # so they are exempt from the dashboard password.
    open_paths = ("/api/webhook/", "/api/accounts/link")
    if not _password_required() or request.url.path.startswith(open_paths):
        return await call_next(request)

    expected = _session_token()
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if cookie and hmac.compare_digest(cookie, expected):
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8", errors="replace")
        except ValueError:
            decoded = ""
        supplied = decoded.partition(":")[2]
        password = state.store.get("server", "dashboard_password", default="")
        if hmac.compare_digest(supplied.encode("utf-8"), password.encode("utf-8")):
            response = await call_next(request)
            response.set_cookie(SESSION_COOKIE, expected, httponly=True,
                                samesite="strict", max_age=30 * 24 * 3600)
            return response

    return Response("Authentication required", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Mail Aggregator"'})


# ---- dashboard --------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(INDEX_PATH, media_type="text/html",
                        headers={"Cache-Control": "no-store"})


# ---- emails -----------------------------------------------------------

@app.get("/api/emails")
async def list_emails(platform: str = "", domain: str = "", account: str = "",
                      q: str = "", unread: int = 0, limit: int = 50, offset: int = 0):
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    total, rows = state.db.query_emails(platform, domain, account, q.strip(),
                                        bool(unread), limit, offset)
    return {"total": total, "emails": rows}


@app.get("/api/emails/{email_id}")
async def get_email(email_id: int):
    row = state.db.get_email(email_id)
    if row is None:
        raise HTTPException(404, "email not found")
    row.pop("dedupe_hash", None)
    return row


@app.post("/api/emails/{email_id}/read")
async def set_read(email_id: int, payload: dict = Body(default={})):
    if not state.db.set_read(email_id, bool(payload.get("is_read", True))):
        raise HTTPException(404, "email not found")
    return {"ok": True}


@app.post("/api/emails/read-all")
async def read_all(payload: dict = Body(default={})):
    count = state.db.mark_all_read(str(payload.get("platform") or ""),
                                   str(payload.get("domain") or ""),
                                   str(payload.get("account") or ""))
    return {"ok": True, "updated": count}


@app.delete("/api/emails/{email_id}")
async def delete_email(email_id: int):
    if not state.db.delete_email(email_id):
        raise HTTPException(404, "email not found")
    return {"ok": True}


@app.get("/api/facets")
async def facets():
    return state.db.facets()


# ---- accounts ---------------------------------------------------------

@app.get("/api/accounts")
async def list_accounts():
    statuses = state.manager.statuses()
    accounts = []
    for account in state.store.accounts():
        view = public_account(account)
        view["status"] = statuses.get(account["id"], {
            "state": "disabled" if not account["enabled"] else "connecting",
            "detail": "", "last_check": "", "fetched": 0})
        if account["platform"] == "instaddr":
            session = state.db.get_instaddr_session(account["id"])
            view["session_linked"] = session is not None
            view["session_validated_at"] = session["validated_at"] if session else ""
        accounts.append(view)
    return {"accounts": accounts, "config_errors": state.store.load_errors}


@app.post("/api/accounts")
async def add_account(payload: dict = Body(...)):
    try:
        account = normalize_account(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    added, updated = state.store.add_accounts([account])
    state.manager.reload()
    return {"ok": True, "added": added, "updated": updated,
            "account": public_account(account)}


@app.post("/api/accounts/import")
async def import_accounts(file: UploadFile = File(...)):
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(413, "file too large")
    text = raw.decode("utf-8-sig", errors="replace")
    name = (file.filename or "").lower()

    errors = []
    accounts = []
    if name.endswith(".json") or text.lstrip().startswith(("{", "[")):
        import json
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise HTTPException(400, "invalid JSON: %s" % exc)
        entries = data.get("accounts", []) if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise HTTPException(400, "JSON must be a list or have an 'accounts' list")
        for index, entry in enumerate(entries):
            try:
                accounts.append(normalize_account(entry))
            except ValueError as exc:
                errors.append("entry %d: %s" % (index + 1, exc))
    else:
        accounts, errors = parse_accounts_csv(text)

    added, updated = state.store.add_accounts(accounts) if accounts else (0, 0)
    if accounts:
        state.manager.reload()
    return {"ok": True, "added": added, "updated": updated, "errors": errors}


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str):
    if not state.store.remove_account(account_id):
        raise HTTPException(404, "account not found")
    state.db.delete_instaddr_session(account_id)
    state.manager.reload()
    return {"ok": True}


@app.get("/api/settings")
async def settings():
    smtp = state.store.get("smtp_receiver", default={})
    return {
        "webhook_path": "/api/webhook/inbound",
        "webhook_secret": state.store.get("webhook_secret", default=""),
        "link_path": "/api/accounts/link",
        "link_token": state.store.get("link_token", default=""),
        "smtp_enabled": bool(smtp.get("enabled")),
        "smtp_port": smtp.get("port"),
        "forward_domains": state.store.get("instaddr", "forward_domains", default=[]),
        "debug_dump": bool(state.store.get("instaddr", "debug_dump", default=False)),
        "platforms": PLATFORM_LABELS,
    }


@app.post("/api/settings/debug")
async def set_debug(payload: dict = Body(default={})):
    """Toggle saving of raw InstAddr pages to debug_out/ for troubleshooting."""
    enabled = bool(payload.get("enabled"))
    state.store.set_value(("instaddr", "debug_dump"), enabled)
    return {"ok": True, "debug_dump": enabled}


def _cors(body):
    # Allow the bookmarklet (running on m.kuku.lu) to call this cross-origin.
    body.headers["Access-Control-Allow-Origin"] = "*"
    body.headers["Access-Control-Allow-Headers"] = "*"
    body.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return body


@app.options("/api/accounts/link")
async def link_preflight():
    return _cors(JSONResponse({"ok": True}))


async def _read_link_body(request):
    """Collect a link payload from JSON, text, form or query. Never raises."""
    data = {}
    try:
        raw = await request.body()
    except Exception:
        raw = b""
    text = raw.decode("utf-8", errors="replace").strip() if raw else ""
    if text:
        if text[0] in "{[":
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    data = dict(parsed)
            except ValueError:
                data = {}
        if not data and ("=" in text and "&" in text):
            # urlencoded form body.
            from urllib.parse import parse_qsl
            data = dict(parse_qsl(text))
        if not data:
            # A raw cookie string posted as the whole body.
            data = {"cookies": text}
    if not data:
        try:
            form = await request.form()
            data = {k: v for k, v in form.items() if isinstance(v, str)}
        except Exception:
            data = {}
    # Query params always win (the bookmarklet carries the token there and
    # may carry fields too).
    data.update({k: v for k, v in request.query_params.items()})
    return data


@app.post("/api/accounts/link")
async def link_instaddr(request: Request):
    """Zero-trust linking: attach a session captured in the user's own browser.

    Authorized by link_token (query ?token= or X-Link-Token header), never the
    dashboard password, so the bookmarklet can post cross-origin. Robustly
    accepts JSON, form, a raw cookie string, or query fields; validates the
    session against m.kuku.lu instantly; stores it in emails.db. No config.json
    editing required.
    """
    token = state.store.get("link_token", default="")
    supplied = (request.headers.get("x-link-token")
                or request.query_params.get("token") or "")
    if not token or not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
        return _cors(JSONResponse({"ok": False, "detail": "invalid link token"}, status_code=401))

    raw_data = await _read_link_body(request)
    payload = parse_link_payload(raw_data)
    if not payload["session_hash"]:
        return _cors(JSONResponse(
            {"ok": False, "detail": "no session cookie found. Paste the "
             "cookie_sessionhash value, or use the bookmarklet on a logged-in "
             "m.kuku.lu tab."}, status_code=400))

    # Resolve (or create) the account identity in config.
    account_id = payload["account_id"]
    email = payload["email"]
    if not account_id:
        if not email:
            email = "instaddr-" + payload["session_hash"][:8]
        account_id = account_id_for("instaddr", email)
    existing = {a["id"]: a for a in state.store.accounts()}
    if account_id not in existing:
        account = normalize_account({"platform": "instaddr", "email": email or account_id,
                                     "mode": "browser", "session_key": payload["session_hash"]})
        account_id = account["id"]
        if payload["proxy"]:
            account["proxy"] = payload["proxy"]
        state.store.add_accounts([account])
    else:
        # Make sure the existing account is browser-mode and carries the proxy.
        state.store.link_instaddr_session(account_id, proxy=payload["proxy"] or None)
        email = email or existing[account_id]["email"]

    # Validate against m.kuku.lu off the event loop (requests is blocking).
    base_url = state.store.get("instaddr", "base_url", default="https://m.kuku.lu")
    validation = await asyncio.get_running_loop().run_in_executor(
        None, lambda: validate_session(base_url, payload["session_hash"],
                                        payload["csrf_token"], payload["user_agent"],
                                        payload["proxy"]))

    # Store the session in the database regardless; status reflects validation.
    state.db.upsert_instaddr_session(
        account_id, email=email, session_hash=payload["session_hash"],
        csrf_token=validation.get("csrf_token") or payload["csrf_token"],
        user_agent=payload["user_agent"], proxy=payload["proxy"])
    if validation["authenticated"]:
        status = "ok"
    elif validation.get("inconclusive"):
        # Pre-check could not decide (bot protection or network); the
        # browser worker verifies the session for real.
        status = "linked"
    else:
        status = "expired"
    state.db.update_instaddr_runtime(account_id, status=status, detail=validation["detail"],
                                     validated=validation["authenticated"])

    state.manager.reload()
    log.info("InstAddr linked: %s (%s)", email, validation["detail"])
    account = {a["id"]: a for a in state.store.accounts()}.get(account_id)
    return _cors(JSONResponse({
        "ok": True,
        "validated": validation["authenticated"],
        "reachable": validation["ok"],
        "pending": bool(validation.get("inconclusive")),
        "detail": validation["detail"],
        "account": public_account(account) if account else {"id": account_id, "email": email},
    }))


# ---- InstAddr Method A (part 2): inbound webhook ---------------------

def _first(mapping, *names):
    for name in names:
        value = mapping.get(name)
        if value:
            return str(value)
    return ""


@app.post("/api/webhook/inbound")
async def webhook_inbound(request: Request):
    """Receive one email pushed by a forwarder.

    Accepted payloads:
      * raw RFC 822 body (Content-Type message/rfc822, text/plain or
        application/octet-stream), recipient in the "to" query parameter
      * JSON: {"raw" | "raw_base64"} or {"from","to","subject","text","html"}
      * form posts in Mailgun / SendGrid inbound-parse style
    """
    secret = state.store.get("webhook_secret", default="")
    supplied = (request.headers.get("x-webhook-secret")
                or request.query_params.get("token") or "")
    if not secret or not hmac.compare_digest(supplied.encode("utf-8"), secret.encode("utf-8")):
        raise HTTPException(401, "invalid webhook secret")

    declared = request.headers.get("content-length", "0")
    if declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
        raise HTTPException(413, "payload too large")

    content_type = request.headers.get("content-type", "").lower()
    platform = request.query_params.get("platform", "instaddr")
    query_to = request.query_params.get("to", "")
    row_id = None

    if "application/json" in content_type:
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(400, "invalid JSON")
        if not isinstance(data, dict):
            raise HTTPException(400, "JSON body must be an object")
        to_addr = _first(data, "to", "recipient") or query_to
        platform = _first(data, "platform") or platform
        raw = None
        if data.get("raw_base64"):
            try:
                raw = base64.b64decode(data["raw_base64"])
            except ValueError:
                raise HTTPException(400, "raw_base64 is not valid base64")
        elif data.get("raw"):
            raw = str(data["raw"]).encode("utf-8", errors="replace")
        if raw is not None:
            row_id = state.ingestor.ingest_mime(raw, platform, to_addr, envelope_to=to_addr)
        else:
            if not to_addr:
                raise HTTPException(400, "missing 'to'")
            row_id = state.ingestor.ingest_fields(
                platform=platform, account_email=to_addr, recipient=to_addr,
                sender=_first(data, "from", "sender"),
                subject=_first(data, "subject"),
                body_text=_first(data, "text", "body_text", "body-plain"),
                body_html=_first(data, "html", "body_html", "body-html"),
                message_id=_first(data, "message_id", "Message-Id"))
    elif "multipart/form-data" in content_type or "x-www-form-urlencoded" in content_type:
        form = await request.form()
        fields = {key: value for key, value in form.items() if isinstance(value, str)}
        to_addr = _first(fields, "recipient", "to") or query_to
        raw_mime = _first(fields, "body-mime", "email")
        if raw_mime:
            row_id = state.ingestor.ingest_mime(
                raw_mime.encode("utf-8", errors="replace"), platform, to_addr,
                envelope_to=to_addr if "@" in to_addr and "<" not in to_addr else "")
        else:
            if not to_addr:
                raise HTTPException(400, "missing recipient")
            row_id = state.ingestor.ingest_fields(
                platform=platform, account_email=to_addr, recipient=to_addr,
                sender=_first(fields, "from", "sender"),
                subject=_first(fields, "subject"),
                body_text=_first(fields, "body-plain", "text"),
                body_html=_first(fields, "body-html", "html"),
                message_id=_first(fields, "Message-Id", "message-id"))
    else:
        raw = await request.body()
        if not raw:
            raise HTTPException(400, "empty body")
        if len(raw) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "payload too large")
        row_id = state.ingestor.ingest_mime(raw, platform, query_to, envelope_to=query_to)

    return JSONResponse({"ok": True, "stored": row_id is not None, "id": row_id})


# ---- live feed --------------------------------------------------------

@app.websocket("/ws")
async def websocket_feed(websocket: WebSocket):
    if _password_required():
        cookie = websocket.cookies.get(SESSION_COOKIE, "")
        if not cookie or not hmac.compare_digest(cookie, _session_token()):
            await websocket.close(code=4401)
            return
    await websocket.accept()
    queue = state.hub.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=25)
            except asyncio.TimeoutError:
                event = {"type": "ping"}
            await websocket.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        state.hub.unsubscribe(queue)
