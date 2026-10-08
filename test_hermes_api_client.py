from __future__ import annotations

import io
import json
import logging
import socket
import time
import urllib.error
import urllib.request

import pytest

import hermes_api_client as api


class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status


def _configure(monkeypatch):
    monkeypatch.setenv(api.API_KEY_ENV, "unit-test-bearer-secret")
    monkeypatch.delenv(api.BASE_URL_ENV, raising=False)
    monkeypatch.delenv(api.HERMES_HOME_ENV, raising=False)


def test_health_uses_default_url_bearer_and_explicit_timeout(monkeypatch):
    _configure(monkeypatch)
    seen = {}

    def fake_urlopen(request, timeout):
        seen.update(
            url=request.full_url,
            method=request.method,
            authorization=request.get_header("Authorization"),
            accept=request.get_header("Accept"),
            timeout=timeout,
        )
        return _Response(b'{"status":"ok","platform":"hermes-agent"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = api.HermesAPIClient(timeout=3.5)

    assert client.health() == {"status": "ok", "platform": "hermes-agent"}
    assert seen == {
        "url": "http://hermes-agent:8642/health",
        "method": "GET",
        "authorization": "Bearer unit-test-bearer-secret",
        "accept": "application/json",
        "timeout": 3.5,
    }


def test_capabilities_uses_environment_base_url_and_constructor_override(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv(api.BASE_URL_ENV, "http://environment-api:9000/root/")
    requested = []

    def fake_urlopen(request, timeout):
        requested.append((request.full_url, request.method, timeout))
        return _Response(b'{"features":{"run_submission":true}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert api.HermesAPIClient().capabilities() == {"features": {"run_submission": True}}
    assert api.HermesAPIClient("http://override-api:9100/api", timeout=2).capabilities() == {
        "features": {"run_submission": True}
    }
    assert requested == [
        ("http://environment-api:9000/root/v1/capabilities", "GET", api.DEFAULT_TIMEOUT_SECONDS),
        ("http://override-api:9100/api/v1/capabilities", "GET", 2.0),
    ]


def test_create_run_posts_nonempty_input_and_accepts_202(monkeypatch):
    _configure(monkeypatch)
    seen = {}

    def fake_urlopen(request, timeout):
        seen.update(
            url=request.full_url,
            method=request.method,
            authorization=request.get_header("Authorization"),
            content_type=request.get_header("Content-type"),
            body=json.loads(request.data),
            timeout=timeout,
        )
        return _Response(b'{"run_id":"run_123","status":"started","replayed":false}', status=202)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = api.HermesAPIClient(timeout=4)

    assert client.create_run("Check the service health") == {
        "run_id": "run_123",
        "status": "started",
        "replayed": False,
    }
    assert seen == {
        "url": "http://hermes-agent:8642/v1/runs",
        "method": "POST",
        "authorization": "Bearer unit-test-bearer-secret",
        "content_type": "application/json",
        "body": {"input": "Check the service health"},
        "timeout": 4.0,
    }


def test_stop_run_posts_without_body_and_accepts_200(monkeypatch):
    _configure(monkeypatch)
    seen = {}
    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, method=request.method, body=request.data,
                    timeout=timeout)
        return _Response(b'{"run_id":"run_123","status":"stopped"}', status=200)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = api.HermesAPIClient().stop_run("run_123", profile="chat")
    assert result == {"run_id": "run_123", "status": "stopped"}
    assert seen == {"url": "http://hermes-agent:8642/p/chat/v1/runs/run_123/stop",
                    "method": "POST", "body": None, "timeout": api.DEFAULT_TIMEOUT_SECONDS}


def test_get_run_returns_completed_status(monkeypatch):
    _configure(monkeypatch)
    seen = []

    def fake_urlopen(request, timeout):
        seen.append((request.full_url, request.method, timeout))
        return _Response(b'{"object":"hermes.run","run_id":"run_123","status":"completed"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = api.HermesAPIClient()

    assert client.get_run("run_123") == {
        "object": "hermes.run",
        "run_id": "run_123",
        "status": "completed",
    }
    assert seen == [("http://hermes-agent:8642/v1/runs/run_123", "GET", api.DEFAULT_TIMEOUT_SECONDS)]


def test_create_run_requires_nonempty_input_and_run_id_is_validated(monkeypatch):
    _configure(monkeypatch)
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: calls.append(args))
    client = api.HermesAPIClient()

    for value in ("", "  ", None, [], {"text": "hello"}):
        with pytest.raises(api.HermesAPIError) as error:
            client.create_run(value)
        assert error.value.category == "invalid_input"

    with pytest.raises(api.HermesAPIError) as error:
        client.get_run("../other")
    assert error.value.category == "invalid_input"
    assert calls == []


def test_http_errors_expose_status_but_never_secret_or_response_body(monkeypatch, caplog):
    _configure(monkeypatch)
    secret = "unit-test-bearer-secret"
    response_secret = "response-body-private-secret"

    def fake_urlopen(_request, timeout):
        raise urllib.error.HTTPError(
            "http://hermes-agent:8642/v1/runs",
            401,
            "unauthorized",
            {},
            io.BytesIO(response_secret.encode()),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = api.HermesAPIClient()

    with caplog.at_level(logging.DEBUG), pytest.raises(api.HermesAPIError) as error:
        client.create_run("hello")

    assert error.value.category == "http_error"
    assert error.value.status_code == 401
    rendered = " ".join((str(error.value), repr(error.value), repr(client), caplog.text))
    assert secret not in rendered
    assert response_secret not in rendered


def test_timeout_and_network_errors_are_sanitized(monkeypatch, caplog):
    _configure(monkeypatch)
    secret = "transport-error-private-secret"

    for cause, category in (
        (socket.timeout(secret), "timeout"),
        (OSError(secret), "network_error"),
    ):
        def fake_urlopen(_request, timeout, *, failure=cause):
            raise failure

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = api.HermesAPIClient()
        with caplog.at_level(logging.DEBUG), pytest.raises(api.HermesAPIError) as error:
            client.health()
        assert error.value.category == category
        rendered = " ".join((str(error.value), repr(error.value), repr(client), caplog.text))
        assert secret not in rendered
        assert "unit-test-bearer-secret" not in rendered


def test_missing_api_key_fails_clearly_without_disclosing_environment(monkeypatch):
    monkeypatch.delenv(api.API_KEY_ENV, raising=False)
    monkeypatch.setenv(api.BASE_URL_ENV, "http://example.test")

    with pytest.raises(api.HermesAPIError, match="api_key_missing") as error:
        api.HermesAPIClient()

    assert error.value.category == "api_key_missing"
    assert api.API_KEY_ENV not in str(error.value)


def test_environment_key_has_priority_over_dotenv(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("API_SERVER_KEY=file-secret\n", encoding="utf-8")
    monkeypatch.setenv(api.HERMES_HOME_ENV, str(tmp_path))
    monkeypatch.setenv(api.API_KEY_ENV, "environment-secret")
    client = api.HermesAPIClient()
    assert "environment-secret" not in repr(client)

    seen = {}
    def fake_urlopen(request, timeout):
        seen["authorization"] = request.get_header("Authorization")
        return _Response(b'{"status":"ok"}')
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client.health()
    assert seen["authorization"] == "Bearer environment-secret"


def test_dotenv_fallback_and_secret_is_not_exposed(monkeypatch, tmp_path, caplog):
    secret = "dotenv-private-secret"
    (tmp_path / ".env").write_text(
        f"OTHER_KEY=wrong\nAPI_SERVER_KEY_EXTRA=wrong\nAPI_SERVER_KEY='{secret}'\n", encoding="utf-8"
    )
    monkeypatch.delenv(api.API_KEY_ENV, raising=False)
    monkeypatch.setenv(api.HERMES_HOME_ENV, str(tmp_path))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(OSError(secret)))
    client = api.HermesAPIClient()
    with caplog.at_level(logging.DEBUG), pytest.raises(api.HermesAPIError) as error:
        client.health()
    assert error.value.category == "network_error"
    assert secret not in " ".join((str(error.value), repr(error.value), repr(client), caplog.text))


def test_create_session_posts_minimal_fields_and_accepts_201(monkeypatch):
    _configure(monkeypatch)
    seen = {}
    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, method=request.method, body=json.loads(request.data))
        return _Response(b'{"session_id":"sess_1"}', status=201)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert api.HermesAPIClient().create_session("sess_1", "Title", "cli") == {"session_id": "sess_1"}
    assert seen == {"url": "http://hermes-agent:8642/api/sessions", "method": "POST",
                    "body": {"session_id": "sess_1", "title": "Title", "source": "cli"}}


def test_create_run_session_idempotency_and_profile_routes(monkeypatch):
    _configure(monkeypatch)
    seen = {}
    def fake_urlopen(request, timeout):
        if request.data is None:
            seen["poll_url"] = request.full_url
            return _Response(b'{"run_id":"r1","status":"running"}')
        seen.update(url=request.full_url, body=json.loads(request.data),
                    idempotency=request.get_header("Idempotency-key"))
        return _Response(b'{"run_id":"r1"}', status=202)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = api.HermesAPIClient()
    client.create_run("hello", session_id="s1", profile="chat", idempotency_key="key-123")
    assert seen == {"url": "http://hermes-agent:8642/p/chat/v1/runs",
                    "body": {"input": "hello", "session_id": "s1"}, "idempotency": "key-123"}
    client.get_run("r1", profile="chat")
    assert seen["poll_url"] == "http://hermes-agent:8642/p/chat/v1/runs/r1"


def test_get_session_and_profile_routes(monkeypatch):
    _configure(monkeypatch)
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: (seen.append(req.full_url) or _Response(b'{"id":"s1"}')))
    client = api.HermesAPIClient()
    assert client.get_session("s1", profile="chat") == {"id": "s1"}
    assert seen == ["http://hermes-agent:8642/p/chat/api/sessions/s1"]


def test_profile_session_run_and_idempotency_validation(monkeypatch):
    _configure(monkeypatch)
    client = api.HermesAPIClient()
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    for profile in ("../x", "a/b", "", "x" * 65):
        with pytest.raises(api.HermesAPIError):
            client.health(profile=profile)
    for bad_id in ("../x", "a/b", "", "x" * 129):
        for call in (lambda: client.get_run(bad_id), lambda: client.stop_run(bad_id),
                     lambda: client.get_session(bad_id),
                     lambda: client.create_run("hello", session_id=bad_id),
                     lambda: client.create_session(session_id=bad_id)):
            with pytest.raises(api.HermesAPIError):
                call()
    for key in ("", "a b", "a\n", "é", "x" * 256):
        with pytest.raises(api.HermesAPIError):
            client.create_run("hello", idempotency_key=key)
    assert calls == []


class _SSEResponse(io.BytesIO):
    status = 200


def test_iter_run_events_decodes_frames_and_terminal_event(monkeypatch):
    _configure(monkeypatch)
    response = _SSEResponse(
        b": keepalive\nunknown: ignored\nevent: run.progress\ndata: {\"n\":1}\n\n"
        b"event: run.completed\ndata: {\"status\":\"completed\"}\n\n"
    )
    seen = {}
    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, accept=request.get_header("Accept"),
                    auth=request.get_header("Authorization"), timeout=timeout)
        return response
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert list(api.HermesAPIClient().iter_run_events("run_1")) == [
        {"n": 1, "event": "run.progress"},
        {"status": "completed", "event": "run.completed"},
    ]
    assert seen == {"url": "http://hermes-agent:8642/v1/runs/run_1/events",
                    "accept": "text/event-stream", "auth": "Bearer unit-test-bearer-secret",
                    "timeout": 15.0}


def test_iter_run_events_frame_limit_resets_between_events(monkeypatch):
    _configure(monkeypatch)
    frame = b'data: {"text":"' + (b"x" * (api.MAX_RESPONSE_BYTES // 2)) + b'"}\n\n'
    response = _SSEResponse(frame + frame)
    seen = {}
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: (seen.update(timeout=timeout) or response))
    events = list(api.HermesAPIClient(timeout=2).iter_run_events("run_1"))
    assert len(events) == 2
    assert len(events[0]["text"]) == api.MAX_RESPONSE_BYTES // 2
    assert seen["timeout"] == 15.0


def test_iter_run_events_profile_route_invalid_json_and_id_validation(monkeypatch):
    _configure(monkeypatch)
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: (seen.append(req.full_url) or
        _SSEResponse(b"event: run.completed\ndata: {nope}\n\n")))
    client = api.HermesAPIClient()
    iterator = client.iter_run_events("run_1", profile="chat")
    with pytest.raises(api.HermesAPIError) as error:
        list(iterator)
    assert error.value.category == "invalid_response"
    assert seen == ["http://hermes-agent:8642/p/chat/v1/runs/run_1/events"]
    seen.clear()
    with pytest.raises(api.HermesAPIError) as error:
        list(client.iter_run_events("../bad"))
    assert error.value.category == "invalid_input"
    assert seen == []


@pytest.mark.parametrize("failure,category", [
    ("unauthorized", "http_error"), ("network", "network_error"), ("timeout", "timeout")
])
def test_iter_run_events_errors_are_sanitized(monkeypatch, caplog, failure, category):
    _configure(monkeypatch)
    secret = "private-transport-secret"
    def fake_urlopen(_request, timeout):
        if failure == "unauthorized":
            raise urllib.error.HTTPError("https://secret.invalid/" + secret, 401, secret, {},
                                          io.BytesIO(secret.encode()))
        if failure == "network":
            raise OSError(secret)
        raise socket.timeout(secret)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = api.HermesAPIClient()
    with caplog.at_level(logging.DEBUG), pytest.raises(api.HermesAPIError) as error:
        list(client.iter_run_events("run_1"))
    assert error.value.category == category
    if failure == "unauthorized":
        assert error.value.status_code == 401
    rendered = " ".join((str(error.value), repr(error.value), repr(client), caplog.text))
    assert secret not in rendered
    assert "unit-test-bearer-secret" not in rendered


def test_iter_run_events_keepalive_only_stream_obeys_deadline(monkeypatch):
    _configure(monkeypatch)
    class KeepaliveResponse(_SSEResponse):
        def __init__(self):
            super().__init__(b"")
            self.reads = 0
        def readline(self, size=-1):
            time.sleep(0.01)
            self.reads += 1
            return b": keepalive\n"
    response = KeepaliveResponse()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: response)
    deadline = time.monotonic() + 0.04
    with pytest.raises(api.HermesAPIError) as error:
        list(api.HermesAPIClient().iter_run_events(
            "run_1", stream_deadline_monotonic=deadline
        ))
    assert error.value.category == "stream_deadline"
    assert 1 <= response.reads <= 6
