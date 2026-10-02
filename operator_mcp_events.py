"""Isolated OpenAI draft MCP Events extension (2026-07-28 only)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import os
import secrets
import socket
import sqlite3
import ssl
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTOCOL = "2026-07-28"
EVENT_NAME = "hermes.test"
MAX_BODY = 256 * 1024
MAX_INSPECT_BYTES = 256 * 1024
_LOG = logging.getLogger("hermes_gpt.mcp_events")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def decode_secret(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise ValueError("invalid_secret")
    try:
        key = base64.b64decode(secret[6:], validate=True)
    except Exception as exc:
        raise ValueError("invalid_secret") from exc
    if not 24 <= len(key) <= 64:
        raise ValueError("invalid_secret")
    return key


def sign(secret: str, message_id: str, timestamp: int, body: bytes) -> str:
    signed = message_id.encode() + b"." + str(timestamp).encode() + b"." + body
    digest = base64.b64encode(hmac.new(decode_secret(secret), signed, hashlib.sha256).digest()).decode()
    return "v1," + digest


def _path() -> Path:
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    path = root / "mcp-events" / "subscriptions.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try: os.chmod(path.parent, 0o700)
    except OSError: pass
    return path


@contextmanager
def _db():
    conn = sqlite3.connect(_path(), timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS subscriptions (id TEXT PRIMARY KEY, principal TEXT NOT NULL, name TEXT NOT NULL, arguments TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL, expires REAL, active INTEGER NOT NULL DEFAULT 1, UNIQUE(principal,url,name,arguments))")
        conn.execute("CREATE TABLE IF NOT EXISTS emitted (event_id TEXT PRIMARY KEY, created REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS callback_verified (principal TEXT NOT NULL, url TEXT NOT NULL, verified_until REAL NOT NULL, PRIMARY KEY(principal,url))")
        conn.commit()
        try: os.chmod(_path(), 0o600)
        except OSError: pass
        yield conn
        conn.commit()
    finally:
        conn.close()


def _resolve_public(host: str, port: int) -> list[str]:
    try: results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc: raise ValueError("dns_failed") from exc
    ips = sorted({r[4][0] for r in results})
    if not ips: raise ValueError("dns_failed")
    for raw in ips:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
        if not ip.is_global: raise ValueError("address_not_public")
    return ips


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address
    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def post_https(url: str, body: bytes, headers: dict[str, str], timeout: float = 5.0) -> tuple[int, bytes]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("invalid_url")
    port = parsed.port or 443
    addresses = _resolve_public(parsed.hostname, port)
    last: Exception | None = None
    for address in addresses:
        conn = _PinnedHTTPS(parsed.hostname, port, address, timeout)
        try:
            target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            conn.request("POST", target, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read(MAX_BODY + 1)
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last = exc
        finally:
            conn.close()
    raise TimeoutError("callback_failed") from last


def _signed_headers(secret: str, msg_id: str, body: bytes, sub_id: str, timestamp: int | None = None) -> dict[str, str]:
    ts = max(int(time.time()), timestamp or 0)
    return {"Content-Type": "application/json", "webhook-id": msg_id,
            "webhook-timestamp": str(ts), "webhook-signature": sign(secret, msg_id, ts, body),
            "X-MCP-Subscription-Id": sub_id}


def verify_callback(url: str, secret: str, sub_id: str) -> None:
    challenge = secrets.token_urlsafe(32)
    body = canonical({"type": "verification", "challenge": challenge})
    try:
        status, response = post_https(url, body, _signed_headers(secret, "msg_verification_" + uuid.uuid4().hex, body, sub_id))
    except TimeoutError: raise ValueError("timeout")
    except ValueError: raise
    except Exception as exc: raise ValueError("connection_failed") from exc
    if not 200 <= status < 300: raise ValueError("http_error")
    try: echoed = json.loads(response).get("challenge", "")
    except Exception as exc: raise ValueError("challenge_failed") from exc
    if not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge): raise ValueError("challenge_failed")


def _principal(headers: list[tuple[bytes, bytes]]) -> str:
    values = {k.lower(): v for k, v in headers}
    auth = values.get(b"authorization", b"")
    # No raw bearer/OAuth credential is ever persisted or logged.
    return hashlib.sha256(auth).hexdigest() if auth else "loopback-anonymous"


def _event_list() -> dict[str, Any]:
    return {"events": [{"name": EVENT_NAME, "description": "Hermes test wake-up event.", "delivery": ["webhook"],
        "inputSchema": {"type":"object","properties":{"test_id":{"type":"string"}},"required":["test_id"],"additionalProperties":False},
        "payloadSchema": {"type":"object","properties":{"test_id":{"type":"string"},"message":{"type":"string"}},"required":["test_id","message"],"additionalProperties":False}}]}


def _rpc_error(req_id: Any, reason: str) -> dict[str, Any]:
    return {"jsonrpc":"2.0","id":req_id,"error":{"code":-32015,"message":"CallbackEndpointError","data":{"reason":reason}}}


def _identity(principal: str, url: str, name: str, args: dict[str, Any]) -> tuple[str,str]:
    canon = canonical(args).decode()
    raw = canonical([principal,url,name,args])
    return "sub_" + hashlib.sha256(raw).hexdigest(), canon

async def dispatch(payload: dict[str, Any], principal: str) -> dict[str, Any] | None:
    method, params, reqid = payload.get("method"), payload.get("params") or {}, payload.get("id")
    if method == "server/discover":
        _LOG.info("rpc method=server/discover modern=true")
        # The modern protocol capability shape is OpenAI's draft extension.
        from versioning import VERSION
        return {"jsonrpc":"2.0","id":reqid,"result":{"resultType":"complete","supportedVersions":[PROTOCOL],"capabilities":{"tools":{},"events":{}},"_meta":{"io.modelcontextprotocol/serverInfo":{"name":"hermes-gpt","version":VERSION}}}}
    if method == "events/list":
        _LOG.info("rpc method=events/list")
        return {"jsonrpc":"2.0","id":reqid,"result":_event_list()}
    if method == "events/subscribe":
        _LOG.info("rpc method=events/subscribe")
        name, args, delivery = params.get("name"), params.get("arguments"), params.get("delivery") or {}
        url, secret = delivery.get("url"), delivery.get("secret")
        if name != EVENT_NAME or not isinstance(args,dict) or set(args)!={"test_id"} or not isinstance(args.get("test_id"),str) or not args["test_id"]:
            return {"jsonrpc":"2.0","id":reqid,"error":{"code":-32602,"message":"Invalid event or arguments"}}
        if delivery.get("mode") != "webhook" or not isinstance(url,str): return _rpc_error(reqid,"invalid_url")
        if not isinstance(secret,str): return _rpc_error(reqid,"invalid_secret")
        try:
            decode_secret(secret)
        except (ValueError, TypeError): return _rpc_error(reqid,"invalid_secret")
        try:
            parsed_url = urllib.parse.urlsplit(url)
            if parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.fragment: return _rpc_error(reqid,"invalid_url")
            _resolve_public(parsed_url.hostname, parsed_url.port or 443)
        except ValueError as exc:
            reason = str(exc) if str(exc) in {"address_not_public","dns_failed"} else "invalid_url"
            return _rpc_error(reqid,reason)
        subid, arg_json = _identity(principal,url,name,args)
        ttl = params.get("ttlMs", 86400000)
        if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0):
            return {"jsonrpc":"2.0","id":reqid,"error":{"code":-32602,"message":"Invalid ttlMs"}}
        expires = None if ttl is None else time.time() + max(60_000, min(30*86400000, ttl)) / 1000
        with _db() as db:
            cached = db.execute("SELECT verified_until FROM callback_verified WHERE principal=? AND url=?",(principal,url)).fetchone()
        try:
            if not cached or cached[0] <= time.time():
                await asyncio.wait_for(asyncio.to_thread(verify_callback,url,secret,subid), timeout=8)
                with _db() as db: db.execute("INSERT INTO callback_verified VALUES(?,?,?) ON CONFLICT(principal,url) DO UPDATE SET verified_until=excluded.verified_until",(principal,url,time.time()+600))
        except TimeoutError:
            _LOG.warning("callback_verification outcome=failed reason=timeout")
            return _rpc_error(reqid,"timeout")
        except ValueError as exc:
            _LOG.warning("callback_verification outcome=failed reason=%s",str(exc))
            return _rpc_error(reqid,str(exc))
        else:
            _LOG.info("callback_verification outcome=passed cached=%s", bool(cached and cached[0] > time.time()))
        with _db() as db:
            db.execute("INSERT INTO subscriptions(id,principal,name,arguments,url,secret,expires,active) VALUES(?,?,?,?,?,?,?,1) ON CONFLICT(principal,url,name,arguments) DO UPDATE SET secret=excluded.secret,expires=excluded.expires,active=1",(subid,principal,name,arg_json,url,secret,expires))
        _LOG.info("subscription state=active id=%s expires=%s", subid, "never" if expires is None else int(expires))
        return {"jsonrpc":"2.0","id":reqid,"result":{"id":subid,"refreshBefore":datetime.fromtimestamp(expires,timezone.utc).isoformat().replace("+00:00","Z") if expires else None,"cursor":None,"truncated":False}}
    if method == "events/unsubscribe":
        _LOG.info("rpc method=events/unsubscribe")
        name,args,delivery=params.get("name"),params.get("arguments"),params.get("delivery") or {}
        if name==EVENT_NAME and isinstance(args,dict) and isinstance(delivery.get("url"),str):
            _, arg_json = _identity(principal,delivery["url"],name,args)
            with _db() as db: db.execute("UPDATE subscriptions SET active=0 WHERE principal=? AND url=? AND name=? AND arguments=?",(principal,delivery["url"],name,arg_json))
        return {"jsonrpc":"2.0","id":reqid,"result":{}}
    return None


def emit_test(test_id: str, message: str, *, event_id: str | None = None, retries: int = 3) -> dict[str,int]:
    """Internal-only emission hook; deliberately not registered as MCP tool/HTTP route."""
    if not isinstance(test_id,str) or not test_id or not isinstance(message,str): raise ValueError("test_id and message required")
    eid = event_id or "evt_" + uuid.uuid4().hex
    with _db() as db:
        if db.execute("SELECT 1 FROM emitted WHERE event_id=?",(eid,)).fetchone(): return {"matched":0,"accepted":0,"duplicate":1}
        db.execute("INSERT INTO emitted VALUES(?,?)",(eid,time.time()))
        rows = db.execute("SELECT id,url,secret FROM subscriptions WHERE active=1 AND name=? AND arguments=? AND (expires IS NULL OR expires>?)",(EVENT_NAME,canonical({"test_id":test_id}).decode(),time.time())).fetchall()
    _LOG.info("event queued event_id=%s matched_subscriptions=%d", eid, len(rows))
    accepted=0
    for subid,url,secret in rows:
        event={"eventId":eid,"name":EVENT_NAME,"timestamp":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),"data":{"test_id":test_id,"message":message},"cursor":None}
        body=canonical(event)
        if len(body)>MAX_BODY: continue
        for attempt in range(max(1,min(retries,5))):
            try:
                headers=_signed_headers(secret,eid,body,subid, int(time.time())+attempt)
                status,_=post_https(url,body,headers)
                if 200<=status<300:
                    accepted+=1
                    _LOG.info("event delivery event_id=%s subscription_id=%s attempt=%d status=%d accepted=true",eid,subid,attempt+1,status)
                    break
                _LOG.warning("event delivery event_id=%s subscription_id=%s attempt=%d status=%d accepted=false",eid,subid,attempt+1,status)
                if status in (410,413): break
                if status<500 and status not in (408,425,429): break
            except (TimeoutError, OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
                _LOG.warning("event delivery event_id=%s subscription_id=%s attempt=%d error=%s",eid,subid,attempt+1,type(exc).__name__)
            if attempt+1<min(retries,5): time.sleep(0.2*(2**attempt))
    return {"matched":len(rows),"accepted":accepted,"duplicate":0}


class EventsASGIMiddleware:
    """Intercept only draft event RPCs on modern protocol; pass all else through."""
    def __init__(self, app: Any, *, enabled: bool = True):
        self.app=app
        self.enabled=enabled
    async def __call__(self,scope:dict[str,Any],receive:Any,send:Any)->None:
        if not self.enabled or scope.get("type")!="http" or scope.get("method")!="POST" or scope.get("path")!="/mcp":
            await self.app(scope,receive,send); return
        headers={k.lower():v for k,v in scope.get("headers",[])}
        buffered=[]
        size=0
        while True:
            msg=await receive()
            buffered.append(msg)
            if msg.get("type")!="http.request":
                break
            size+=len(msg.get("body",b""))
            if size>MAX_INSPECT_BYTES or not msg.get("more_body"):
                break

        def replay_receive_factory():
            index=0
            async def replay_receive():
                nonlocal index
                if index<len(buffered):
                    msg=buffered[index]
                    index+=1
                    return msg
                return await receive()
            return replay_receive

        if size>MAX_INSPECT_BYTES or any(msg.get("type")!="http.request" for msg in buffered):
            await self.app(scope,replay_receive_factory(),send)
            return
        body=b"".join(msg.get("body",b"") for msg in buffered)
        try: payload=json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError): payload=None
        if isinstance(payload,dict):
            method=payload.get("method")
            params=payload.get("params")
            meta=params.get("_meta",{}) if isinstance(params,dict) else {}
            if not isinstance(meta,dict): meta={}
            modern=headers.get(b"mcp-protocol-version")==PROTOCOL.encode() or meta.get("io.modelcontextprotocol/protocolVersion")==PROTOCOL
            if method in {"server/discover","events/list","events/subscribe","events/unsubscribe"} and modern:
                try: result=await dispatch(payload,_principal(scope.get("headers",[])))
                except Exception:  # noqa: BLE001 - map handler failures to generic JSON-RPC error
                    result={"jsonrpc":"2.0","id":payload.get("id"),"error":{"code":-32603,"message":"Internal error"}}
                if result is not None:
                    raw=json.dumps(result,separators=(",",":"),ensure_ascii=False).encode()
                    await send({"type":"http.response.start","status":200,"headers":[(b"content-type",b"application/json"),(b"content-length",str(len(raw)).encode())]})
                    await send({"type":"http.response.body","body":raw}); return
        await self.app(scope,replay_receive_factory(),send)
