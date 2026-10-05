"""Safe JSON-RPC request tracing at the HTTP MCP entry point."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

_LOG = logging.getLogger("hermes_gpt.mcp_rpc_trace")
_PREFIX = "mcp_rpc_trace_json="
_MAX_REQUEST_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
_SAFE_PROTOCOL = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PROTOCOL_META_KEY = "io.modelcontextprotocol/protocolVersion"


def _safe_id(value: Any) -> int | str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and abs(value) <= 9_007_199_254_740_991:
        return value
    if isinstance(value, str):
        raw = value.encode("utf-8", errors="replace")[:1024]
        return "rid_" + hashlib.sha256(raw).hexdigest()[:16]
    return None


def _safe_method(value: Any) -> str:
    if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value):
        return value
    return "unparsed"


def _safe_protocol(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_PROTOCOL.fullmatch(value):
        return value
    return None


def _request_records(raw: bytes, *, oversized: bool) -> list[tuple[str, Any, str | None, str]]:
    if oversized:
        return [("unparsed", None, None, "none")]
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return []
    values = payload if isinstance(payload, list) else [payload]
    records = []
    for item in values:
        if not isinstance(item, dict) or item.get("jsonrpc") != "2.0" or "method" not in item:
            continue
        method = _safe_method(item.get("method"))
        request_id = _safe_id(item.get("id")) if "id" in item else None
        params = item.get("params")
        protocol = None
        source = "none"
        if isinstance(params, dict):
            meta = params.get("_meta")
            if isinstance(meta, dict):
                protocol = _safe_protocol(meta.get(_PROTOCOL_META_KEY))
                if protocol:
                    source = "meta"
            if protocol is None and method == "initialize":
                protocol = _safe_protocol(params.get("protocolVersion"))
                if protocol:
                    source = "meta"
        records.append((method, request_id, protocol, source))
    return records


def _rpc_error_codes(raw: bytearray, *, content_type: bytes | None, oversized: bool) -> list[int | None]:
    if oversized or not content_type or not content_type.lower().startswith(b"application/json"):
        return []
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return []
    values = payload if isinstance(payload, list) else [payload]
    result: list[int | None] = []
    for item in values:
        error = item.get("error") if isinstance(item, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        result.append(
            code if isinstance(code, int) and not isinstance(code, bool) and abs(code) <= 2**31 - 1 else None
        )
    return result


class MCPRPCTraceASGIMiddleware:
    """Observe JSON-RPC metadata without logging request/response contents."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/mcp"
        ):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        header_protocol = _safe_protocol(
            headers.get(b"mcp-protocol-version", b"").decode("ascii", errors="ignore")
        )
        request_body = bytearray()
        request_oversized = False
        request_complete = False
        response_body = bytearray()
        response_oversized = False
        response_content_type: bytes | None = None
        http_status: int | None = None
        start = time.monotonic()

        async def traced_receive() -> dict[str, Any]:
            nonlocal request_oversized, request_complete
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if not request_oversized:
                    remaining = _MAX_REQUEST_BYTES + 1 - len(request_body)
                    if len(body) > remaining:
                        request_oversized = True
                        request_body.clear()
                    else:
                        request_body.extend(body)
                        if len(request_body) > _MAX_REQUEST_BYTES:
                            request_oversized = True
                            request_body.clear()
                if not message.get("more_body", False):
                    request_complete = True
            return message

        async def traced_send(message: dict[str, Any]) -> None:
            nonlocal http_status, response_content_type, response_oversized
            if message.get("type") == "http.response.start":
                http_status = message.get("status")
                for key, value in message.get("headers", []):
                    if key.lower() == b"content-type":
                        response_content_type = value
                        break
            elif message.get("type") == "http.response.body" and response_content_type:
                if response_content_type.lower().startswith(b"application/json") and not response_oversized:
                    body = message.get("body", b"")
                    remaining = _MAX_RESPONSE_BYTES + 1 - len(response_body)
                    if len(body) > remaining:
                        response_oversized = True
                        response_body.clear()
                    else:
                        response_body.extend(body)
                        if len(response_body) > _MAX_RESPONSE_BYTES:
                            response_oversized = True
                            response_body.clear()
            await send(message)

        failure = False
        try:
            await self.app(scope, traced_receive, traced_send)
        except BaseException:
            failure = True
            raise
        finally:
            if request_complete:
                records = _request_records(bytes(request_body), oversized=request_oversized)
            elif request_body:
                records = _request_records(bytes(request_body), oversized=True)
            else:
                records = []
            error_codes = _rpc_error_codes(
                response_body,
                content_type=response_content_type,
                oversized=response_oversized,
            )
            duration_ms = max(0, round((time.monotonic() - start) * 1000, 3))
            path = "/mcp"
            for index, (method, request_id, meta_protocol, meta_source) in enumerate(records):
                protocol = header_protocol or meta_protocol
                detection_source = "header" if header_protocol else (meta_source if meta_protocol else "none")
                error_code = error_codes[index] if index < len(error_codes) else None
                if failure:
                    outcome = "transport_error"
                elif error_code is not None:
                    outcome = "rpc_error"
                elif isinstance(http_status, int) and http_status >= 400:
                    outcome = "http_error"
                elif isinstance(http_status, int):
                    outcome = "success"
                else:
                    outcome = "transport_error"
                record = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "http_path": path,
                    "rpc_method": method,
                    "protocol_version": protocol,
                    "detection_source": detection_source,
                    "request_id": request_id,
                    "http_status": http_status,
                    "rpc_error_code": error_code,
                    "outcome": outcome,
                    "duration_ms": duration_ms,
                }
                _LOG.info("%s%s", _PREFIX, json.dumps(record, separators=(",", ":"), allow_nan=False))
