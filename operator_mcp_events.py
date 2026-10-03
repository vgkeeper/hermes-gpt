"""OpenAI MCP Events projection over the durable Hermes Live Events journal."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import secrets
import socket
import sqlite3
import ssl
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_live_events as live_events

PROTOCOL = "2026-07-28"
EVENT_NAME = "hermes.live_event"
MAX_BODY = 256 * 1024
MAX_INSPECT_BYTES = 256 * 1024
MAX_SUBSCRIPTIONS = 20
PROJECT_BATCH = 100
PROJECTOR_POLL_SECONDS = 1.0
_LOG = logging.getLogger("hermes_gpt.mcp_events")


class UnsupportedCallbackResponse(TypeError):
    """The callback challenge response was not a supported JSON object."""


_PROJECTORS: dict[str, tuple[threading.Event, threading.Thread]] = {}
_PROJECTOR_LOCK = threading.Lock()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def decode_secret(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise ValueError("invalid_secret")
    try:
        key = base64.b64decode(secret[6:], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid_secret") from exc
    if not 24 <= len(key) <= 64:
        raise ValueError("invalid_secret")
    return key


def sign(secret: str, message_id: str, timestamp: int, body: bytes) -> str:
    signed = message_id.encode() + b"." + str(timestamp).encode() + b"." + body
    digest = base64.b64encode(hmac.new(decode_secret(secret), signed, hashlib.sha256).digest()).decode()
    return "v1," + digest


def _path(hermes_root: Path | None = None) -> Path:
    root = live_events._root(hermes_root)
    path = root / "mcp-events" / "subscriptions.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    return path


@contextmanager
def _db(hermes_root: Path | None = None):
    path = _path(hermes_root)
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS subscriptions ("
            "id TEXT PRIMARY KEY, principal TEXT NOT NULL, name TEXT NOT NULL, "
            "arguments TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL, "
            "expires REAL, active INTEGER NOT NULL DEFAULT 1, cursor INTEGER NOT NULL DEFAULT 0, "
            "truncated INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL, "
            "UNIQUE(principal,url,name,arguments))"
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(subscriptions)")}
        for name, declaration in (
            ("cursor", "INTEGER NOT NULL DEFAULT 0"),
            ("truncated", "INTEGER NOT NULL DEFAULT 0"),
            ("lease_token", "TEXT"),
            ("lease_until", "REAL"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE subscriptions ADD COLUMN {name} {declaration}")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS callback_verified ("
            "principal TEXT NOT NULL, url TEXT NOT NULL, secret_fingerprint TEXT NOT NULL DEFAULT '', "
            "verified_until REAL NOT NULL, PRIMARY KEY(principal,url))"
        )
        verified_columns = {row[1] for row in conn.execute("PRAGMA table_info(callback_verified)")}
        if "secret_fingerprint" not in verified_columns:
            conn.execute("ALTER TABLE callback_verified ADD COLUMN secret_fingerprint TEXT NOT NULL DEFAULT ''")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='emitted'").fetchone():
            conn.execute("DROP TABLE emitted")
        conn.commit()
        try:
            path.chmod(0o600)
        except OSError:
            pass
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
    except TimeoutError as exc:
        raise ValueError("timeout") from exc
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise ValueError("connection_failed") from exc
    if not 200 <= status < 300:
        raise ValueError("http_error")
    try:
        payload = json.loads(response)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("challenge_failed") from exc
    if not isinstance(payload, dict):
        raise UnsupportedCallbackResponse("challenge_failed")
    echoed = payload.get("challenge", "")
    if not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
        raise ValueError("challenge_failed")


def _principal(headers: list[tuple[bytes, bytes]]) -> str:
    values = {k.lower(): v for k, v in headers}
    auth = values.get(b"authorization", b"")
    # No raw bearer/OAuth credential is ever persisted or logged.
    return hashlib.sha256(auth).hexdigest() if auth else "loopback-anonymous"


def _event_list() -> dict[str, Any]:
    properties = {
        "mission_id": {"type": "string", "maxLength": live_events.MAX_SUBJECT},
        "topic": {"type": "string", "maxLength": live_events.MAX_TOPIC},
        "kind": {"type": "string", "maxLength": live_events.MAX_KIND},
    }
    event_properties = {
        "event_id": {"type": "string"},
        "seq": {"type": "integer"},
        "topic": {"type": "string"},
        "kind": {"type": "string"},
        "subject_type": {"type": "string"},
        "subject_id": {"type": "string"},
        "mission_id": {"type": "string"},
        "source": {"type": "string"},
        "created_at": {"type": "string"},
        "payload": {"type": "object"},
    }
    return {
        "events": [
            {
                "name": EVENT_NAME,
                "description": "Wake-up notification from Live Events; re-read durable state before acting.",
                "delivery": ["webhook"],
                "inputSchema": {"type": "object", "properties": properties, "additionalProperties": False},
                "payloadSchema": {
                    "type": "object",
                    "properties": event_properties,
                    "required": list(event_properties),
                    "additionalProperties": False,
                },
            }
        ]
    }


def _rpc_error(req_id: Any, reason: str) -> dict[str, Any]:
    return {"jsonrpc":"2.0","id":req_id,"error":{"code":-32015,"message":"CallbackEndpointError","data":{"reason":reason}}}


def _identity(principal: str, url: str, name: str, args: dict[str, Any]) -> tuple[str, str]:
    encoded_args = canonical(args).decode()
    raw = canonical([principal, url, name, args])
    return "sub_" + hashlib.sha256(raw).hexdigest(), encoded_args


def _parse_filters(arguments: Any) -> dict[str, str] | None:
    if not isinstance(arguments, dict) or set(arguments) - {"mission_id", "topic", "kind"}:
        return None
    bounds = {
        "mission_id": live_events.MAX_SUBJECT,
        "topic": live_events.MAX_TOPIC,
        "kind": live_events.MAX_KIND,
    }
    try:
        return {
            key: live_events._bounded_ref(value, key, bounds[key])
            for key, value in arguments.items()
        }
    except (TypeError, ValueError):
        return None


def _bounded_cursor(value: Any, high: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isdecimal() and len(value) <= 19:
        value = int(value)
    if not isinstance(value, int) or value < 0 or value > live_events.MAX_CURSOR or value > high:
        return None
    return value


def _subscription_cursor(params: dict[str, Any], hermes_root: Path | None) -> tuple[int, bool] | None:
    oldest, high = live_events.cursor_bounds(hermes_root)
    value = params.get("cursor")
    cursor = high if value is None else _bounded_cursor(value, high)
    if cursor is None:
        return None
    if oldest and cursor < oldest - 1:
        return oldest - 1, True
    return cursor, False


def _safe_url(url: Any) -> tuple[str, int] | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            return None
        return parsed.hostname, parsed.port or 443
    except ValueError:
        return None


async def dispatch(
    payload: dict[str, Any], principal: str, hermes_root: Path | None = None
) -> dict[str, Any] | None:
    method, params, reqid = payload.get("method"), payload.get("params") or {}, payload.get("id")
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": reqid, "error": {"code": -32602, "message": "Invalid params"}}
    if method == "server/discover":
        from versioning import VERSION

        return {"jsonrpc": "2.0", "id": reqid, "result": {"resultType": "complete", "supportedVersions": [PROTOCOL], "capabilities": {"tools": {}, "events": {}}, "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "hermes-gpt", "version": VERSION}}}}
    if method == "events/list":
        return {"jsonrpc": "2.0", "id": reqid, "result": _event_list()}
    if method == "events/subscribe":
        name = params.get("name")
        args = _parse_filters(params.get("arguments"))
        delivery = params.get("delivery") or {}
        if name != EVENT_NAME or args is None:
            return {"jsonrpc": "2.0", "id": reqid, "error": {"code": -32602, "message": "Invalid event or arguments"}}
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook":
            return _rpc_error(reqid, "invalid_url")
        url, secret = delivery.get("url"), delivery.get("secret")
        target = _safe_url(url)
        if target is None:
            return _rpc_error(reqid, "invalid_url")
        if not isinstance(secret, str):
            return _rpc_error(reqid, "invalid_secret")
        try:
            decode_secret(secret)
            _resolve_public(*target)
        except ValueError as exc:
            reason = str(exc) if str(exc) in {"address_not_public", "dns_failed", "invalid_secret"} else "invalid_url"
            return _rpc_error(reqid, reason)
        ttl = params.get("ttlMs", 86_400_000)
        if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0):
            return {"jsonrpc": "2.0", "id": reqid, "error": {"code": -32602, "message": "Invalid ttlMs"}}
        expires = None if ttl is None else time.time() + max(60_000, min(30 * 86_400_000, ttl)) / 1000
        subid, args_json = _identity(principal, url, name, args)
        secret_fingerprint = hashlib.sha256(secret.encode()).hexdigest()
        with _db(hermes_root) as db:
            cached = db.execute(
                "SELECT verified_until FROM callback_verified WHERE principal=? AND url=? AND secret_fingerprint=?",
                (principal, url, secret_fingerprint),
            ).fetchone()
        try:
            if not cached or cached[0] <= time.time():
                await asyncio.wait_for(asyncio.to_thread(verify_callback, url, secret, subid), timeout=8)
                with _db(hermes_root) as db:
                    db.execute(
                        "INSERT INTO callback_verified(principal,url,secret_fingerprint,verified_until) VALUES(?,?,?,?) "
                        "ON CONFLICT(principal,url) DO UPDATE SET secret_fingerprint=excluded.secret_fingerprint,verified_until=excluded.verified_until",
                        (principal, url, secret_fingerprint, time.time() + 600),
                    )
        except TimeoutError:
            return _rpc_error(reqid, "timeout")
        except (UnsupportedCallbackResponse, ValueError) as exc:
            return _rpc_error(reqid, str(exc))
        except (OSError, sqlite3.Error, ssl.SSLError, http.client.HTTPException):
            return _rpc_error(reqid, "connection_failed")
        with _db(hermes_root) as db:
            existing = db.execute(
                "SELECT cursor,truncated FROM subscriptions WHERE principal=? AND url=? AND name=? AND arguments=?",
                (principal, url, name, args_json),
            ).fetchone()
            active_count = db.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE principal=? AND active=1 AND (expires IS NULL OR expires>?)",
                (principal, time.time()),
            ).fetchone()[0]
            if existing is None and active_count >= MAX_SUBSCRIPTIONS:
                return _rpc_error(reqid, "subscription_limit")
            start_cursor = _subscription_cursor(params, hermes_root)
            if start_cursor is None:
                return {"jsonrpc": "2.0", "id": reqid, "error": {"code": -32602, "message": "Invalid cursor"}}
            cursor, truncated = start_cursor
            if existing is not None:
                if params.get("cursor") is None:
                    cursor = int(existing["cursor"])
                    truncated = bool(existing["truncated"])
                else:
                    cursor = max(cursor, int(existing["cursor"]))
                    truncated = truncated or bool(existing["truncated"])
            db.execute(
                "INSERT INTO subscriptions(id,principal,name,arguments,url,secret,expires,active,cursor,truncated) "
                "VALUES(?,?,?,?,?,?,?,1,?,?) ON CONFLICT(principal,url,name,arguments) DO UPDATE SET "
                "secret=excluded.secret,expires=excluded.expires,active=1,"
                "cursor=MAX(subscriptions.cursor,excluded.cursor),"
                "truncated=MAX(subscriptions.truncated,excluded.truncated),lease_token=NULL,lease_until=NULL",
                (subid, principal, name, args_json, url, secret, expires, cursor, int(truncated)),
            )
            saved = db.execute("SELECT cursor,truncated FROM subscriptions WHERE id=?", (subid,)).fetchone()
        start_projector(hermes_root)
        return {
            "jsonrpc": "2.0",
            "id": reqid,
            "result": {
                "id": subid,
                "refreshBefore": datetime.fromtimestamp(expires, timezone.utc).isoformat().replace("+00:00", "Z") if expires else None,
                "cursor": str(saved["cursor"]),
                "truncated": bool(saved["truncated"]),
            },
        }
    if method == "events/unsubscribe":
        name = params.get("name")
        args = _parse_filters(params.get("arguments"))
        delivery = params.get("delivery") or {}
        if name == EVENT_NAME and args is not None and isinstance(delivery, dict) and isinstance(delivery.get("url"), str):
            _, args_json = _identity(principal, delivery["url"], name, args)
            with _db(hermes_root) as db:
                db.execute(
                    "UPDATE subscriptions SET active=0,lease_token=NULL,lease_until=NULL "
                    "WHERE principal=? AND url=? AND name=? AND arguments=?",
                    (principal, delivery["url"], name, args_json),
                )
        return {"jsonrpc": "2.0", "id": reqid, "result": {}}
    return None


def _project_event(event: dict[str, Any]) -> dict[str, Any]:
    data = {key: event[key] for key in (
        "event_id", "seq", "topic", "kind", "subject_type", "subject_id",
        "mission_id", "source", "created_at", "payload",
    )}
    return {
        "eventId": event["event_id"],
        "name": EVENT_NAME,
        "timestamp": event["created_at"],
        "data": data,
        "cursor": str(event["seq"]),
    }


def _deliver(row: sqlite3.Row, event: dict[str, Any], retries: int = 3) -> bool:
    body = canonical(_project_event(event))
    if len(body) > MAX_BODY:
        return False
    message_id = event["event_id"]
    for attempt in range(max(1, min(int(retries), 5))):
        try:
            headers = _signed_headers(row["secret"], message_id, body, row["id"], int(time.time()) + attempt)
            status, _ = post_https(row["url"], body, headers)
            if 200 <= status < 300:
                return True
            if status in (410, 413) or (status < 500 and status not in (408, 425, 429)):
                return False
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
            _LOG.warning("event projection failed subscription_id=%s error=%s", row["id"], type(exc).__name__)
        if attempt + 1 < min(retries, 5):
            time.sleep(0.2 * (2**attempt))
    return False


def _claim(subscription_id: str, hermes_root: Path | None) -> tuple[sqlite3.Row, str] | None:
    now = time.time()
    token = uuid.uuid4().hex
    with _db(hermes_root) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM subscriptions WHERE id=? AND active=1 AND (expires IS NULL OR expires>?)",
            (subscription_id, now),
        ).fetchone()
        if row is None or (row["lease_until"] is not None and row["lease_until"] > now):
            return None
        db.execute(
            "UPDATE subscriptions SET lease_token=?,lease_until=? WHERE id=?",
            (token, now + 60, subscription_id),
        )
        return row, token


def _checkpoint(
    subscription_id: str, token: str, cursor: int, truncated: bool, hermes_root: Path | None
) -> bool:
    with _db(hermes_root) as db:
        changed = db.execute(
            "UPDATE subscriptions SET cursor=MAX(cursor,?),truncated=MAX(truncated,?) "
            "WHERE id=? AND active=1 AND lease_token=?",
            (cursor, int(truncated), subscription_id, token),
        )
        return changed.rowcount == 1


def _renew_claim(subscription_id: str, token: str, hermes_root: Path | None) -> bool:
    with _db(hermes_root) as db:
        updated = db.execute(
            "UPDATE subscriptions SET lease_until=? WHERE id=? AND active=1 AND lease_token=?",
            (time.time() + 60, subscription_id, token),
        )
        return updated.rowcount == 1


def _drain(subscription_id: str, hermes_root: Path | None, limit: int = PROJECT_BATCH) -> dict[str, int]:
    stats = {"matched": 0, "accepted": 0, "pending": 0, "duplicate": 0, "truncated": 0}
    claimed = _claim(subscription_id, hermes_root)
    if claimed is None:
        return stats
    row, token = claimed
    cursor = int(row["cursor"])
    was_truncated = bool(row["truncated"])
    try:
        oldest, high = live_events.cursor_bounds(hermes_root)
        if oldest and cursor < oldest - 1:
            cursor = oldest - 1
            was_truncated = True
            stats["truncated"] = 1
            if not _checkpoint(subscription_id, token, cursor, True, hermes_root):
                return stats
        events, _ = live_events.read_since(cursor, limit=max(1, min(int(limit), live_events.MAX_QUERY)), hermes_root=hermes_root)
        if not events:
            if high > cursor:
                stats["truncated"] = 1
                _checkpoint(subscription_id, token, high, True, hermes_root)
            return stats
        filters = json.loads(row["arguments"])
        for event in events:
            seq = int(event["seq"])
            if seq > cursor + 1:
                was_truncated = True
                stats["truncated"] = 1
            if all(not expected or event.get(key) == expected for key, expected in filters.items()):
                stats["matched"] += 1
                if not _renew_claim(subscription_id, token, hermes_root):
                    stats["pending"] = 1
                    break
                if not _deliver(row, event):
                    stats["pending"] = 1
                    break
                stats["accepted"] += 1
            cursor = seq
            if not _checkpoint(subscription_id, token, cursor, was_truncated, hermes_root):
                break
    finally:
        with _db(hermes_root) as db:
            db.execute("UPDATE subscriptions SET lease_token=NULL,lease_until=NULL WHERE id=? AND lease_token=?", (subscription_id, token))
    return stats


def project_pending(hermes_root: Path | None = None) -> dict[str, int]:
    """Project durable Live Events to subscriptions; callbacks never re-enter the journal."""
    totals = {"matched": 0, "accepted": 0, "pending": 0, "duplicate": 0, "truncated": 0}
    try:
        with _db(hermes_root) as db:
            rows = db.execute(
                "SELECT id FROM subscriptions WHERE active=1 AND (expires IS NULL OR expires>?) ORDER BY id LIMIT ?",
                (time.time(), MAX_SUBSCRIPTIONS * 10),
            ).fetchall()
        for row in rows:
            for key, value in _drain(row["id"], hermes_root).items():
                totals[key] += value
    except (OSError, sqlite3.Error, ValueError) as exc:
        _LOG.warning("event projection scan failed error=%s", type(exc).__name__)
    return totals


def _projector_loop(root: Path, wake: threading.Event) -> None:
    while True:
        wake.wait(PROJECTOR_POLL_SECONDS)
        wake.clear()
        result = project_pending(root)
        if result["pending"]:
            time.sleep(PROJECTOR_POLL_SECONDS)
            wake.set()


def start_projector(hermes_root: Path | None = None) -> None:
    path = live_events._root(hermes_root) / "mcp-events" / "subscriptions.sqlite3"
    if not path.is_file():
        return
    root = live_events._root(hermes_root)
    key = str(root.resolve())
    with _PROJECTOR_LOCK:
        current = _PROJECTORS.get(key)
        if current is not None and current[1].is_alive():
            current[0].set()
            return
        wake = threading.Event()
        thread = threading.Thread(target=_projector_loop, args=(root, wake), daemon=True)
        _PROJECTORS[key] = (wake, thread)
        wake.set()
        thread.start()


def notify_projector(hermes_root: Path | None = None) -> None:
    key = str(live_events._root(hermes_root).resolve())
    with _PROJECTOR_LOCK:
        projector = _PROJECTORS.get(key)
        if projector is not None:
            projector[0].set()


class EventsASGIMiddleware:
    """Intercept draft Events RPCs on modern protocol; pass all else through."""

    def __init__(
        self,
        app: Any,
        *,
        enabled: bool = True,
        hermes_root_getter: Callable[[], Path | None] | None = None,
    ):
        self.app = app
        self.enabled = enabled
        self.hermes_root_getter = hermes_root_getter

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not self.enabled or scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        buffered: list[dict[str, Any]] = []
        size = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message.get("type") != "http.request":
                break
            size += len(message.get("body", b""))
            if size > MAX_INSPECT_BYTES or not message.get("more_body"):
                break

        def replay_receive_factory():
            index = 0

            async def replay_receive():
                nonlocal index
                if index < len(buffered):
                    message = buffered[index]
                    index += 1
                    return message
                return await receive()

            return replay_receive

        if size > MAX_INSPECT_BYTES or any(message.get("type") != "http.request" for message in buffered):
            await self.app(scope, replay_receive_factory(), send)
            return
        body = b"".join(message.get("body", b"") for message in buffered)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            params = payload.get("params")
            meta = params.get("_meta", {}) if isinstance(params, dict) else {}
            if not isinstance(meta, dict):
                meta = {}
            modern = (
                headers.get(b"mcp-protocol-version") == PROTOCOL.encode()
                or meta.get("io.modelcontextprotocol/protocolVersion") == PROTOCOL
            )
            method = payload.get("method")
            if method in {"server/discover", "events/list", "events/subscribe", "events/unsubscribe"} and modern:
                root = self.hermes_root_getter() if self.hermes_root_getter else None
                try:
                    result = await dispatch(payload, _principal(scope.get("headers", [])), root)
                except (OSError, sqlite3.Error):
                    result = {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32603, "message": "Internal error"}}
                if result is not None:
                    raw = json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode()
                    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]})
                    await send({"type": "http.response.body", "body": raw})
                    return
        await self.app(scope, replay_receive_factory(), send)
