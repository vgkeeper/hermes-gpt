import asyncio
import base64
import hashlib
import hmac
import json

import pytest
from starlette.testclient import TestClient

import operator_live_events as live
import operator_mcp_events as ev

SECRET = "whsec_" + base64.b64encode(b"s" * 32).decode()
SECRET_ALT = "whsec_" + base64.b64encode(b"t" * 32).decode()


def test_standard_webhooks_signature_exact_body():
    body=b'{"x":1}'
    sig=ev.sign(SECRET,"evt_1",123,body)
    expected=base64.b64encode(hmac.new(b"s"*32,b"evt_1.123."+body,hashlib.sha256).digest()).decode()
    assert sig=="v1,"+expected
    assert sig != ev.sign(SECRET,"evt_1",123,b'{ "x": 1 }')


def test_secret_constraints():
    for key in (b"x"*23,b"x"*65):
        with pytest.raises(ValueError): ev.decode_secret("whsec_"+base64.b64encode(key).decode())


def test_ssrf_rejects_non_public(monkeypatch):
    monkeypatch.setattr(ev.socket,"getaddrinfo",lambda *a,**kw:[(2,1,6,"",("127.0.0.1",443))])
    with pytest.raises(ValueError,match="address_not_public"): ev._resolve_public("attacker.example",443)


def test_projection_uses_event_id_for_webhook_and_retry_signature(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    monkeypatch.setattr(ev.time, "sleep", lambda *_: None)
    with ev._db(root) as db:
        db.execute(
            "INSERT INTO subscriptions(id,principal,name,arguments,url,secret,expires,active,cursor) "
            "VALUES(?,?,?,?,?,?,?,1,0)",
            ("sub_retry", "p", ev.EVENT_NAME, ev.canonical({"topic": "test"}).decode(), "https://example.com/cb", SECRET, None),
        )
    live.publish_event(
        topic="test", kind="job.completed", subject_type="job", subject_id="job-1",
        source="test", payload={"status": "complete", "prompt": "hidden"}, hermes_root=root,
    )
    attempts = []

    def respond(_url, body, headers):
        attempts.append((json.loads(body), headers))
        return (503, b"") if len(attempts) == 1 else (200, b"")

    monkeypatch.setattr(ev, "post_https", respond)
    result = ev._drain("sub_retry", root)
    assert result["accepted"] == 1 and result["pending"] == 0 and len(attempts) == 2
    assert len(attempts) == 2
    for projected, headers in attempts:
        assert projected["eventId"] == headers["webhook-id"]
        body = ev.canonical(projected)
        timestamp = int(headers["webhook-timestamp"])
        assert headers["webhook-signature"] == ev.sign(SECRET, projected["eventId"], timestamp, body)
    first_headers, second_headers = (attempt[1] for attempt in attempts)
    assert first_headers["webhook-id"] == second_headers["webhook-id"]
    assert first_headers["webhook-timestamp"] != second_headers["webhook-timestamp"]
    assert first_headers["webhook-signature"] != second_headers["webhook-signature"]
    assert {k:v for k,v in first_headers.items() if k not in {"webhook-timestamp", "webhook-signature"}} == \
           {k:v for k,v in second_headers.items() if k not in {"webhook-timestamp", "webhook-signature"}}
    assert attempts[0][0]["data"]["payload"]["prompt"] == "[REDACTED]"
    assert ev._drain("sub_retry", root)["accepted"] == 0
    assert len(attempts) == 2


def test_callback_verification_success_and_failure(monkeypatch):
    seen={}
    def post(url,body,headers):
        seen.update(json.loads(body)); seen.update(headers)
        return 200,json.dumps({"challenge":seen["challenge"]}).encode()
    monkeypatch.setattr(ev,"post_https",post)
    ev.verify_callback("https://public.example/cb",SECRET,"sub_abc")
    assert seen["type"]=="verification"
    assert seen["webhook-signature"].startswith("v1,")
    assert seen["X-MCP-Subscription-Id"]=="sub_abc"
    monkeypatch.setattr(ev,"post_https",lambda *a:(200,b'{"challenge":"no"}'))
    with pytest.raises(ValueError,match="challenge_failed"): ev.verify_callback("https://public.example/cb",SECRET,"sub_abc")

def test_subscription_cursor_is_bounded_and_retention_is_reported(monkeypatch):
    monkeypatch.setattr(live, "cursor_bounds", lambda _root=None: (5, 9))
    assert ev._subscription_cursor({}, None) == (9, False)
    assert ev._subscription_cursor({"cursor": "0"}, None) == (4, True)
    assert ev._subscription_cursor({"cursor": 9}, None) == (9, False)
    assert ev._subscription_cursor({"cursor": 10}, None) is None
    assert ev._subscription_cursor({"cursor": "not-a-cursor"}, None) is None




def test_subscribe_projects_live_event_cursor_and_deduplicates(tmp_path, monkeypatch):
    async def run():
        root = tmp_path / "hermes"
        calls = []
        monkeypatch.setattr(ev, "verify_callback", lambda *args: calls.append(("verify", args)))
        monkeypatch.setattr(ev, "_resolve_public", lambda *args: ["93.184.216.34"])
        monkeypatch.setattr(ev, "start_projector", lambda *_: None)
        monkeypatch.setattr(
            ev,
            "post_https",
            lambda url, body, headers: (calls.append(("deliver", json.loads(body), headers)) or (200, b"")),
        )
        principal = "principal-hash"
        params = {
            "name": ev.EVENT_NAME,
            "arguments": {"topic": "test"},
            "cursor": "0",
            "delivery": {"mode": "webhook", "url": "https://public.example/cb", "secret": SECRET},
        }
        one = await ev.dispatch({"id": 1, "method": "events/subscribe", "params": params}, principal, root)
        params["delivery"]["secret"] = SECRET_ALT
        changed_secret = await ev.dispatch(
            {"id": 3, "method": "events/subscribe", "params": params}, principal, root
        )
        assert changed_secret["result"]["id"] == one["result"]["id"]
        assert len([call for call in calls if call[0] == "verify"]) == 2
        params["delivery"]["secret"] = SECRET

        two = await ev.dispatch({"id": 2, "method": "events/subscribe", "params": params}, principal, root)
        assert one["result"]["id"] == two["result"]["id"]
        assert one["result"]["refreshBefore"]
        assert one["result"]["cursor"] == "0"
        with ev._db(root) as db:
            assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 1

        live.publish_event(
            topic="test", kind="job.completed", subject_type="job", subject_id="job-1",
            source="test", payload={"state": "done"}, hermes_root=root,
        )
        high_before = live.high_watermark(root)
        projection = ev.project_pending(root)
        assert projection["matched"] == projection["accepted"] == 1
        event_call = next(call for call in calls if call[0] == "deliver")
        assert event_call[1]["name"] == ev.EVENT_NAME
        assert event_call[1]["data"]["event_id"]
        assert event_call[1]["cursor"] == "1"
        assert event_call[2]["webhook-id"] == event_call[1]["eventId"]
        assert ev.project_pending(root)["accepted"] == 0
        assert live.high_watermark(root) == high_before
        assert len(live.read_since(0, hermes_root=root)[0]) == 1
        fresh = await ev.dispatch(
            {"id": 4, "method": "events/subscribe", "params": {
                "name": ev.EVENT_NAME, "arguments": {"topic": "other"},
                "delivery": {"mode": "webhook", "url": "https://public.example/cb", "secret": SECRET},
            }},
            principal,
            root,
        )
        assert fresh["result"]["cursor"] == str(high_before)



        unsub = await ev.dispatch(
            {"id": 3, "method": "events/unsubscribe", "params": {
                "name": ev.EVENT_NAME, "arguments": {"topic": "test"},
                "delivery": {"url": "https://public.example/cb"},
            }},
            principal,
            root,
        )
        assert unsub["result"] == {}
        live.publish_event(
            topic="test", kind="job.updated", subject_type="job", subject_id="job-1",
            source="test", payload={"state": "running"}, hermes_root=root,
        )
        assert ev.project_pending(root)["accepted"] == 0
        assert len([call for call in calls if call[0] == "deliver"]) == 1

    asyncio.run(run())


def test_events_rpc_and_modern_only_passthrough():
    async def run():
        result=await ev.dispatch({"id":1,"method":"server/discover","params":{}},"p")
        assert result["result"]["capabilities"]["events"]=={}
        listing=await ev.dispatch({"id":2,"method":"events/list","params":{}},"p")
        assert listing["result"]["events"][0]["name"]==ev.EVENT_NAME
        assert listing["result"]["events"][0]["delivery"]==["webhook"]
    asyncio.run(run())


def test_real_http_modern_events(monkeypatch, tmp_path):
    from mcp_compat import SDK_V2

    if not SDK_V2:
        pytest.skip(
            "MCP SDK 1.x rejects protocol 2026-07-28 before Events middleware; "
            "the modern Events protocol requires SDK 2.x"
        )

    import server
    import versioning

    monkeypatch.setenv("HERMES_GPT_ENABLE_MCP", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    built = server.build_server(http=True)
    app = server.build_asgi_app(built, http=True)
    with TestClient(app, base_url="http://127.0.0.1:7677") as client:
        modern = {"Accept":"application/json, text/event-stream", "MCP-Protocol-Version":"2026-07-28"}
        discover = client.post("/mcp", headers=modern, json={"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}})
        assert discover.status_code == 200, discover.text
        assert discover.headers["content-type"].startswith("application/json")
        assert discover.json() == {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "resultType": "complete", "supportedVersions": [ev.PROTOCOL],
                "capabilities": {"tools": {}, "events": {}},
                "_meta": {"io.modelcontextprotocol/serverInfo": {
                    "name": "hermes-gpt", "version": versioning.VERSION
                }},
            },
        }
        listing = client.post("/mcp", headers=modern, json={"jsonrpc":"2.0","id":2,"method":"events/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}})
        assert listing.status_code == 200
        assert listing.headers["content-type"].startswith("application/json")
        assert listing.json()["result"]["events"][0] == ev._event_list()["events"][0]
        tools = client.post("/mcp", headers={**modern,"MCP-Method":"tools/list"}, json={"jsonrpc":"2.0","id":3,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}})
        assert tools.status_code == 200
        assert tools.headers["content-type"].startswith("application/json")
        assert tools.json()["result"]["tools"]


def test_real_http_legacy_tools_compat(monkeypatch, tmp_path):
    import server

    monkeypatch.setenv("HERMES_GPT_ENABLE_MCP", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    built = server.build_server(http=True)
    app = server.build_asgi_app(built, http=True)
    with TestClient(app, base_url="http://127.0.0.1:7677") as client:
        accept = "application/json, text/event-stream"
        initialized = client.post("/mcp", headers={"Accept": accept}, json={
            "jsonrpc":"2.0", "id":1, "method":"initialize",
            "params":{"protocolVersion":"2025-11-25", "capabilities":{},
                      "clientInfo":{"name":"pytest", "version":"1"}},
        })
        assert initialized.status_code == 200, initialized.text
        assert initialized.json()["result"]["protocolVersion"] == "2025-11-25"
        legacy = {"Accept": accept, "MCP-Protocol-Version":"2025-11-25"}
        tools = client.post("/mcp", headers=legacy, json={
            "jsonrpc":"2.0", "id":2, "method":"tools/list", "params":{},
        })
        assert tools.status_code == 200, tools.text
        assert tools.json()["result"]["tools"]
        events = client.post("/mcp", headers=legacy, json={
            "jsonrpc":"2.0", "id":3, "method":"events/list", "params":{},
        })
        assert events.status_code == 200
        assert "result" not in events.json()



def test_modern_events_rpc_logging_is_redacted(caplog, monkeypatch):
    async def run():
        async def downstream(*_args):
            raise AssertionError("modern event method should be intercepted")
        async def fake_dispatch(payload, _principal, _root):
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"events": []}}
        monkeypatch.setattr(ev, "dispatch", fake_dispatch)
        middleware = ev.EventsASGIMiddleware(downstream)
        request = json.dumps({"jsonrpc":"2.0", "id":"scan-17", "method":"events/list",
                              "params":{"_meta":{"io.modelcontextprotocol/protocolVersion":ev.PROTOCOL},
                                        "delivery":{"url":"https://private.invalid/callback", "secret":"callback-secret"},
                                        "prompt":"private prompt"}}).encode()
        sent = []
        messages = [{"type":"http.request", "body":request, "more_body":False}]
        async def receive(): return messages.pop(0)
        async def send(message): sent.append(message)
        scope={"type":"http", "method":"POST", "path":"/mcp", "headers":[
            (b"mcp-protocol-version", ev.PROTOCOL.encode()),
            (b"authorization", b"Bearer private-bearer") ]}
        await middleware(scope, receive, send)
        assert sent[0]["status"] == 200
        assert dict(sent[0]["headers"])[b"content-type"] == b"application/json"
        for method in ("server/discover", "events/subscribe", "events/unsubscribe"):
            messages.append({"type":"http.request", "body":json.dumps({
                "jsonrpc":"2.0", "id":method, "method":method,
                "params":{"_meta":{"io.modelcontextprotocol/protocolVersion":ev.PROTOCOL},
                          "delivery":{"url":"https://private.invalid/callback", "secret":"callback-secret"},
                          "prompt":"private prompt"}}).encode(), "more_body":False})
            await middleware(scope, receive, send)
    with caplog.at_level("INFO", logger="hermes_gpt.mcp_events"):
        asyncio.run(run())
    records = [r.message for r in caplog.records if "mcp_events_rpc" in r.message]
    assert len(records) == 4
    for method in ("server/discover", "events/list", "events/subscribe", "events/unsubscribe"):
        assert any("method=" + method in record and "outcome=success" in record for record in records)
    record = records[1]
    assert "protocol=2026-07-28" in record and "detection_source=header+meta" in record
    assert "request_id=rid_" in record
    assert "scan-17" not in record
    for forbidden in ("private-bearer", "callback-secret", "private.invalid", "private prompt"):
        assert all(forbidden not in record for record in records)


def test_middleware_streams_oversized_request_to_core_without_event_dispatch(monkeypatch):
    async def run():
        first = b"x" * (ev.MAX_INSPECT_BYTES + 1)
        second = b"tail"
        original_calls = 0
        order = []
        received = bytearray()

        async def fail_dispatch(*_args, **_kwargs):
            raise AssertionError("oversized request must not enter Events dispatch")

        monkeypatch.setattr(ev, "dispatch", fail_dispatch)

        async def receive():
            nonlocal original_calls
            if original_calls == 0:
                original_calls += 1
                order.append("original_first")
                return {"type": "http.request", "body": first, "more_body": True}
            original_calls += 1
            order.append("original_second")
            return {"type": "http.request", "body": second, "more_body": False}

        async def downstream(_scope, downstream_receive, downstream_send):
            order.append("downstream_start")
            while True:
                message = await downstream_receive()
                received.extend(message.get("body", b""))
                if not message.get("more_body"):
                    break
            await downstream_send({"type": "http.response.start", "status": 200, "headers": []})
            await downstream_send({"type": "http.response.body", "body": b"ok"})

        sent = []

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [(b"mcp-protocol-version", ev.PROTOCOL.encode())],
        }
        middleware = ev.EventsASGIMiddleware(downstream)
        await middleware(scope, receive, send)
        assert bytes(received) == first + second
        assert order.index("downstream_start") < order.index("original_second")
        assert sent[0]["status"] == 200

    asyncio.run(run())
