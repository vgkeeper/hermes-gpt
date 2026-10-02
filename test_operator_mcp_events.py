import asyncio
import base64
import hashlib
import hmac
import json

import pytest
from starlette.testclient import TestClient

import operator_mcp_events as ev

SECRET = "whsec_" + base64.b64encode(b"s" * 32).decode()


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


def test_delivery_retry_reuses_id_and_refreshes_signature_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME",str(tmp_path))
    monkeypatch.setattr(ev,"_resolve_public",lambda *a:["93.184.216.34"])
    monkeypatch.setattr(ev.time,"sleep",lambda *_:None)
    with ev._db() as db:
        db.execute("INSERT INTO subscriptions(id,principal,name,arguments,url,secret,expires,active) VALUES(?,?,?,?,?,?,?,1)",("sub_retry","p",ev.EVENT_NAME,ev.canonical({"test_id":"r"}).decode(),"https://example.com/cb",SECRET,None))
    attempts=[]
    def respond(_url,body,headers):
        attempts.append((json.loads(body),headers))
        return (503,b"") if len(attempts)<2 else (200,b"")
    monkeypatch.setattr(ev,"post_https",respond)
    result=ev.emit_test("r","retry",retries=2)
    assert result["accepted"]==1 and len(attempts)==2
    assert attempts[0][0]["eventId"]==attempts[1][0]["eventId"]
    assert attempts[0][1]["webhook-timestamp"] != attempts[1][1]["webhook-timestamp"]
    assert attempts[0][1]["webhook-signature"] != attempts[1][1]["webhook-signature"]


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


def test_subscribe_persist_idempotence_unsubscribe_and_emission(tmp_path,monkeypatch):
    async def run():
        monkeypatch.setenv("HERMES_HOME",str(tmp_path))
        calls=[]
        monkeypatch.setattr(ev,"verify_callback",lambda *a:calls.append(("verify",a)))
        monkeypatch.setattr(ev,"_resolve_public",lambda *a:["93.184.216.34"])
        monkeypatch.setattr(ev,"post_https",lambda url,body,headers:(calls.append(("deliver",json.loads(body),headers)) or (200,b"")))
        principal="principal-hash"
        params={"name":"hermes.test","arguments":{"test_id":"abc"},"delivery":{"mode":"webhook","url":"https://public.example/cb","secret":SECRET}}
        one=await ev.dispatch({"id":1,"method":"events/subscribe","params":params},principal)
        two=await ev.dispatch({"id":2,"method":"events/subscribe","params":params},principal)
        assert one["result"]["id"]==two["result"]["id"]
        assert one["result"]["refreshBefore"]
        with ev._db() as db:
            assert db.execute("select count(*) from subscriptions").fetchone()[0]==1
        assert ev.emit_test("abc","hello")=={"matched":1,"accepted":1,"duplicate":0}
        event_call=next(c for c in calls if c[0]=="deliver")
        assert event_call[1]["data"]=={"test_id":"abc","message":"hello"}
        assert event_call[2]["webhook-id"]==event_call[1]["eventId"]
        assert ev.emit_test("abc","again",event_id=event_call[1]["eventId"])["duplicate"]==1
        unsub=await ev.dispatch({"id":3,"method":"events/unsubscribe","params":{"name":"hermes.test","arguments":{"test_id":"abc"},"delivery":{"url":"https://public.example/cb"}}},principal)
        assert unsub["result"]=={}
        assert ev.emit_test("abc","after")["matched"]==0
    asyncio.run(run())


def test_events_rpc_and_modern_only_passthrough():
    async def run():
        result=await ev.dispatch({"id":1,"method":"server/discover","params":{}},"p")
        assert result["result"]["capabilities"]["events"]=={}
        listing=await ev.dispatch({"id":2,"method":"events/list","params":{}},"p")
        assert listing["result"]["events"][0]["name"]=="hermes.test"
        assert listing["result"]["events"][0]["delivery"]==["webhook"]
    asyncio.run(run())


def test_real_http_modern_events_and_legacy_tools_compat(monkeypatch, tmp_path):
    import server
    monkeypatch.setenv("HERMES_GPT_ENABLE_MCP", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    built = server.build_server(http=True)
    app = server.build_asgi_app(built, http=True)
    with TestClient(app, base_url="http://127.0.0.1:7677") as client:
        modern = {"Accept":"application/json, text/event-stream", "MCP-Protocol-Version":"2026-07-28"}
        discover = client.post("/mcp", headers=modern, json={"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}})
        assert discover.status_code == 200, discover.text
        assert discover.json()["result"]["capabilities"]["events"] == {}
        listing = client.post("/mcp", headers=modern, json={"jsonrpc":"2.0","id":2,"method":"events/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}})
        assert listing.json()["result"]["events"][0]["name"] == "hermes.test"
        tools = client.post("/mcp", headers={**modern,"MCP-Method":"tools/list"}, json={"jsonrpc":"2.0","id":3,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}})
        assert tools.status_code == 200
        assert tools.json()["result"]["tools"]
        legacy = client.post("/mcp", headers={"Accept":"application/json, text/event-stream"}, json={"jsonrpc":"2.0","id":4,"method":"events/list","params":{}})
        assert legacy.status_code == 200
        assert "result" not in legacy.json()


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
