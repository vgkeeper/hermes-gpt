import io
import json
import urllib.request

import operator_work_bridge as bridge


def configure(monkeypatch):
    monkeypatch.setenv(bridge.URL_ENV, "http://hermes-work-bridge:8000")
    monkeypatch.setenv(bridge.TOKEN_ENV, "test-token")


def test_register_is_fixed_authenticated_bridge_request(monkeypatch):
    configure(monkeypatch)
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["method"] = req.method
        seen["headers"] = dict(req.header_items())
        seen["body"] = json.loads(req.data)
        seen["timeout"] = timeout
        return io.BytesIO(b'{"mission_id":"demo_m001","enabled":true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = json.loads(bridge.hermes_work_mission_register("demo", "demo_m001", "sessionA", "jobA"))
    assert result["success"] is True
    assert seen["url"] == "http://hermes-work-bridge:8000/v1/missions"
    assert seen["method"] == "POST"
    assert seen["headers"]["Authorization"] == "Bearer test-token"
    assert seen["body"] == {"project_id": "demo", "mission_id": "demo_m001", "session_id": "sessionA", "job_id": "jobA"}
    assert seen["timeout"] == bridge.TIMEOUT_SECONDS


def test_update_get_cancel_routes_and_validation(monkeypatch):
    configure(monkeypatch)
    seen = []

    def fake_urlopen(req, timeout):
        seen.append((req.method, req.full_url, json.loads(req.data) if req.data else None))
        return io.BytesIO(b'{"ok":true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert json.loads(bridge.hermes_work_mission_update_job("demo_m001", "jobB"))["success"]
    assert json.loads(bridge.hermes_work_mission_get("demo_m001"))["success"]
    assert json.loads(bridge.hermes_work_mission_cancel("demo_m001"))["success"]
    assert seen == [
        ("PUT", "http://hermes-work-bridge:8000/v1/missions/demo_m001/job", {"job_id": "jobB"}),
        ("GET", "http://hermes-work-bridge:8000/v1/missions/demo_m001", None),
        ("POST", "http://hermes-work-bridge:8000/v1/missions/demo_m001/cancel", None),
    ]
    assert json.loads(bridge.hermes_work_mission_register("demo", "other_m001", "s", "j"))["code"] == "INVALID_MISSION_ID"
    assert json.loads(bridge.hermes_work_mission_get("../escape"))["code"] == "INVALID_MISSION_ID"


def test_bridge_is_fail_closed_when_unconfigured(monkeypatch):
    monkeypatch.delenv(bridge.URL_ENV, raising=False)
    monkeypatch.delenv(bridge.TOKEN_ENV, raising=False)
    monkeypatch.delenv(bridge.TOKEN_FILE_ENV, raising=False)
    assert json.loads(bridge.hermes_work_mission_get("demo_m001"))["code"] == "BRIDGE_NOT_CONFIGURED"


def test_rejects_non_tls_external_url(monkeypatch):
    monkeypatch.setenv(bridge.URL_ENV, "http://example.org")
    monkeypatch.setenv(bridge.TOKEN_ENV, "test-token")
    result = json.loads(bridge.hermes_work_mission_get("demo_m001"))
    assert result == {"success": False, "code": "BRIDGE_UNAVAILABLE", "detail": "ValueError"}


def test_token_file_fallback_is_scoped_and_not_returned(monkeypatch, tmp_path):
    configure(monkeypatch)
    monkeypatch.delenv(bridge.TOKEN_ENV)
    token_file = tmp_path / "bridge-token"
    token_file.write_text("file-only-test-token" + chr(10), encoding="utf-8")
    monkeypatch.setenv(bridge.TOKEN_FILE_ENV, str(token_file))
    seen = {}

    def fake_urlopen(req, timeout):
        seen["authorization"] = req.get_header("Authorization")
        return io.BytesIO(b'{"ok":true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = json.loads(bridge.hermes_work_mission_get("demo_m001"))
    assert result["success"] is True
    assert seen["authorization"] == "Bearer file-only-test-token"
    assert "file-only-test-token" not in json.dumps(result)


def test_token_environment_takes_precedence_over_file(monkeypatch, tmp_path):
    configure(monkeypatch)
    token_file = tmp_path / "bridge-token"
    token_file.write_text("file-token", encoding="utf-8")
    monkeypatch.setenv(bridge.TOKEN_FILE_ENV, str(token_file))
    seen = {}

    def fake_urlopen(req, timeout):
        seen["authorization"] = req.get_header("Authorization")
        return io.BytesIO(b'{"ok":true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert json.loads(bridge.hermes_work_mission_get("demo_m001"))["success"]
    assert seen["authorization"] == "Bearer test-token"
