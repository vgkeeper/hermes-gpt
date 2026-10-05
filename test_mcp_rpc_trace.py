import asyncio
import json
import logging

from mcp_rpc_trace import MCPRPCTraceASGIMiddleware

PREFIX = "mcp_rpc_trace_json="
EXPECTED_KEYS = {
    "timestamp", "http_path", "rpc_method", "protocol_version",
    "detection_source", "request_id", "http_status", "rpc_error_code",
    "outcome", "duration_ms",
}


def _run_request(payload, headers=()):
    request = json.dumps(payload).encode()
    messages = [{"type": "http.request", "body": request, "more_body": False}]
    sent = []

    async def receive():
        return messages.pop(0)

    async def app(scope, receive, send):
        await receive()
        await send({
            "type": "http.response.start", "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        body = json.dumps({
            "jsonrpc": "2.0", "id": "private-response-id",
            "error": {"code": -32602, "message": "private callback payload"},
        }).encode()
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "method": "POST", "path": "/mcp",
        "headers": list(headers) + [(b"authorization", b"Bearer private-bearer")],
    }
    asyncio.run(MCPRPCTraceASGIMiddleware(app)(scope, receive, send))
    return sent


def test_trace_is_structured_allowlisted_and_redacted(caplog):
    requests = [
        ({"jsonrpc": "2.0", "id": "host-scan-private", "method": "initialize",
          "params": {"protocolVersion": "2025-11-25", "prompt": "private prompt"}}, [], "initialize", "meta"),
        ({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {"payload": "private"}}, [], "notifications/initialized", "none"),
        ({"jsonrpc": "2.0", "id": 2, "method": "server/discover",
          "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
                     "delivery": {"url": "https://private.invalid/callback", "secret": "private-secret"}}}, [], "server/discover", "meta"),
        ({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {"principal": "private"}},
         [(b"mcp-protocol-version", b"2026-07-28")], "tools/list", "header"),
        ({"jsonrpc": "2.0", "id": 4, "method": "events/list", "params": {}}, [], "events/list", "none"),
        ({"jsonrpc": "2.0", "id": 5, "method": "events/subscribe", "params": {"callback": "private"}}, [], "events/subscribe", "none"),
        ({"jsonrpc": "2.0", "id": 6, "method": "events/unsubscribe", "params": {}}, [], "events/unsubscribe", "none"),
        ({"jsonrpc": "2.0", "id": 7, "method": "custom/future_method", "params": {"payload": "private"}}, [], "custom/future_method", "none"),
    ]
    logger = logging.getLogger("hermes_gpt.mcp_rpc_trace")
    with caplog.at_level(logging.INFO, logger=logger.name):
        for payload, headers, _method, _source in requests:
            sent = _run_request(payload, headers)
            assert sent[0]["status"] == 200
    lines = [record.message for record in caplog.records if record.message.startswith(PREFIX)]
    assert len(lines) == len(requests)
    for line, (payload, headers, method, source) in zip(lines, requests):
        record = json.loads(line.removeprefix(PREFIX))
        assert set(record) == EXPECTED_KEYS
        assert record["http_path"] == "/mcp"
        assert record["rpc_method"] == method
        assert record["detection_source"] == source
        assert record["http_status"] == 200
        assert record["rpc_error_code"] == -32602
        assert record["outcome"] == "rpc_error"
        assert isinstance(record["duration_ms"], (int, float))
        assert record["duration_ms"] >= 0
        assert isinstance(record["timestamp"], str) and record["timestamp"].endswith("+00:00")
        assert record["protocol_version"] == (
            "2026-07-28" if headers else ("2025-11-25" if method == "initialize" else
            "2026-07-28" if method == "server/discover" else None)
        )
        if payload.get("id") == "host-scan-private":
            assert record["request_id"].startswith("rid_")
            assert "host-scan-private" not in line
        if method == "notifications/initialized":
            assert record["request_id"] is None
        for secret in (
            "authorization", "bearer", "private-bearer", "private prompt", "private-secret",
            "private.invalid", "callback", "principal", "payload", "params",
            "private-response-id", "private callback payload",
        ):
            assert secret not in line.lower()


def test_non_mcp_path_is_passed_through_without_trace(caplog):
    called = []

    async def app(scope, receive, send):
        called.append(scope["path"])

    scope = {"type": "http", "method": "POST", "path": "/oauth/token", "headers": []}
    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}
    async def send(_message):
        pass

    with caplog.at_level(logging.INFO, logger="hermes_gpt.mcp_rpc_trace"):
        asyncio.run(MCPRPCTraceASGIMiddleware(app)(scope, receive, send))
    assert called == ["/oauth/token"]
    assert not any(record.message.startswith(PREFIX) for record in caplog.records)


def test_rpc_trace_handles_http_errors_and_non_jsonrpc_bodies(caplog):
    payload = {"jsonrpc": "2.0", "id": "error-id", "method": "other/method", "params": {}}
    request = json.dumps(payload).encode()
    messages = [{"type": "http.request", "body": request, "more_body": False}]
    async def receive(): return messages.pop(0)
    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"private error response", "more_body": False})
    async def send(_message): pass
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}
    with caplog.at_level(logging.INFO, logger="hermes_gpt.mcp_rpc_trace"):
        asyncio.run(MCPRPCTraceASGIMiddleware(app)(scope, receive, send))
    lines = [r.message for r in caplog.records if r.message.startswith(PREFIX)]
    assert len(lines) == 1
    record = json.loads(lines[0][len(PREFIX):])
    assert record["rpc_method"] == "other/method"
    assert record["http_status"] == 503
    assert record["rpc_error_code"] is None
    assert record["outcome"] == "http_error"
    assert "private error response" not in lines[0]
