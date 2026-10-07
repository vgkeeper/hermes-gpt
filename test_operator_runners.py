from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import operator_contract as contract_mod
import operator_policy as op
import operator_runners as runners


def _contract(ws: Path, *, backend: str | None = None, options: dict | None = None) -> dict:
    value = {
        "schema": "hermes.work-contract/v1",
        "task_id": "runner-test-001",
        "assigned_agent": "coder",
        "assigned_profile": "default",
        "objective": "Inspect the workspace and make the requested bounded change.",
        "allowed_scope": {"workspaces": [str(ws)], "profiles": ["default"]},
        "forbidden_actions": [],
        "expected_artifacts": [],
        "tests": [],
        "review_requirements": {},
        "completion_criteria": {
            "run_state": {"terminal": True, "outcome_ok": ["completed"]},
            "artifacts_present": False,
            "tests_pass": False,
            "review_satisfied": False,
            "no_forbidden_actions": True,
        },
        "inputs": [],
        "constraints": [],
        "authorization": {
            "class": "reversible_write",
            "approved": True,
            "approved_by": "owner",
            "approval_reference": "test",
        },
    }
    if backend:
        value["execution"] = {"backend": backend, "options": options or {}}
    return value


def _readonly_remote_agent() -> dict:
    rules = [{"permission": "*", "pattern": "*", "action": "deny"}]
    rules.extend(
        {"permission": tool, "pattern": "*", "action": "deny"}
        for tool in runners._OPENCODE_REMOTE_DENIED_TOOLS
    )
    for tool in ("read", "glob", "grep", "list"):
        rules.append({"permission": tool, "pattern": "*", "action": "allow"})
        rules.extend(
            {"permission": tool, "pattern": pattern, "action": "deny"}
            for pattern in (
                "../*", "/proc/**", *_patterns_for_remote_test(),
            )
        )
    return {
        "name": "hermes-readonly",
        "mode": "primary",
        "model": {"providerID": "hermes-proxy", "modelID": "openai/gpt-6-luna"},
        "permission": rules,
    }


def _patterns_for_remote_test() -> tuple[str, ...]:
    return runners._OPENCODE_REMOTE_SECRET_PATTERNS


def _remote_readonly_contract(ws: Path, *, options: dict | None = None) -> dict:
    value = _contract(ws, backend="opencode", options=options)
    value["authorization"] = {"class": "read_only", "approved": True}
    return value


def _remote_request_stub(*, agent: dict | None = None):
    def request(method: str, path: str, *, body=None, authenticated=True):
        if path == "/global/health":
            if not authenticated:
                return {"unauthenticated": True}
            return {"healthy": True, "version": runners.OPENCODE_REMOTE_SUPPORTED_VERSION}
        if path.startswith("/file/content?"):
            return {"type": "text", "content": runners.OPENCODE_REMOTE_MARKER_VALUE + "\n"}
        if path.startswith("/path?"):
            query = runners.urllib.parse.parse_qs(runners.urllib.parse.urlsplit(path).query)
            directory = query["directory"][0]
            return {"directory": directory, "worktree": directory, "home": "/home/opencode", "state": "/state", "config": "/config"}
        if path.startswith("/config?"):
            return {"default_agent": runners.OPENCODE_REMOTE_AGENT, "model": runners.OPENCODE_REMOTE_MODEL}
        if path.startswith("/agent?"):
            return [agent or _readonly_remote_agent()]
        if path.startswith("/session/") and "/abort?" in path:
            return None
        if path.startswith("/session/"):
            session_id = path.split("/session/", 1)[1].split("?", 1)[0]
            query = runners.urllib.parse.parse_qs(runners.urllib.parse.urlsplit(path).query)
            return {
                "id": session_id,
                "agent": runners.OPENCODE_REMOTE_AGENT,
                "directory": query["directory"][0],
                "model": {"providerID": "hermes-proxy", "modelID": "openai/gpt-6-luna"},
                "permission": None,
            }
        if path.startswith("/session/") and path.endswith("/abort?"):
            return None
        raise AssertionError((method, path, body, authenticated))
    return request



def _enable_workspace(monkeypatch: pytest.MonkeyPatch, ws: Path) -> None:
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PATHS_ENV, str(ws))


def _enable_pi_confinement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub a usable boundary in tests that are not exercising confinement itself."""
    monkeypatch.setattr(
        runners.confinement,
        "confinement_available",
        lambda *, writable=True, expose_proc=False: True,
    )
    monkeypatch.setattr(
        runners.confinement,
        "wrap_argv",
        lambda argv, workspace, *, writable=True, expose_proc=False: list(argv),
    )


def test_builtin_backends_registered():
    names = {item["name"] for item in runners.list_backends()}
    assert {"fleet", "pi_rpc", "opencode", "omx", "codex"}.issubset(names)


def test_legacy_contract_hash_shape_unchanged_without_execution(tmp_path: Path):
    raw = _contract(tmp_path)
    canonical, parsed, sha = contract_mod._parse_contract(json.dumps(raw))
    assert "execution" not in parsed
    assert "execution" not in json.loads(canonical)
    assert len(sha) == 64
    assert runners.selected_backend(parsed) == "fleet"


def test_execution_is_canonical_and_surface_redacts_option_values(tmp_path: Path):
    raw = _contract(tmp_path, backend="pi_rpc", options={"model": "test/model", "provider": "test-provider"})
    _, parsed, _ = contract_mod._parse_contract(json.dumps(raw))
    assert parsed["execution"]["backend"] == "pi_rpc"
    surface = contract_mod._surface_contract(parsed)
    assert surface["execution"] == {"backend": "pi_rpc", "option_keys": ["model", "provider"]}
    assert "test/model" not in json.dumps(surface)


def test_unknown_backend_returns_structured_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _enable_workspace(monkeypatch, tmp_path)
    raw = _contract(tmp_path, backend="does_not_exist")
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=tmp_path / "hermes"))
    assert payload["success"] is False
    assert payload["code"] == "RUNNER_BACKEND_UNKNOWN"
    assert payload["backend"] == "does_not_exist"


def test_pi_rpc_dry_run_uses_rpc_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    _enable_pi_confinement(monkeypatch)
    backend = runners.get_backend("pi_rpc")
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")
    raw = _contract(ws, backend="pi_rpc", options={"model": "x/y"})
    raw["authorization"] = {"class": "read_only", "approved": True}
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=root))
    assert payload["success"] is True
    assert payload["dry_run"] is True
    assert payload["backend"] == "pi_rpc"
    assert payload["plan"]["protocol"] == "jsonl-rpc"
    assert payload["plan"]["mode"] == "rpc"
    assert payload["plan"]["model"] == "x/y"


def test_pi_defaults_and_profile_credential_reference_are_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pi_dir = tmp_path / "pi-agent"
    pi_dir.mkdir()
    (pi_dir / "settings.json").write_text(
        json.dumps({"defaultProvider": "test-provider", "defaultModel": "test-model"}),
        encoding="utf-8",
    )
    (pi_dir / "models.json").write_text(
        json.dumps({"providers": {"test-provider": {"apiKey": "$PI_TEST_PROVIDER_KEY", "models": [{"id": "test-model"}]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_dir))
    monkeypatch.delenv("PI_TEST_PROVIDER_KEY", raising=False)
    monkeypatch.setenv("PI_TEST_UNRELATED", "parent-secret-must-not-copy")

    hermes_root = tmp_path / "hermes"
    hermes_root.mkdir()
    (hermes_root / ".env").write_text(
        "PI_TEST_PROVIDER_KEY=test-credential\nPI_TEST_UNRELATED=do-not-copy\n",
        encoding="utf-8",
    )
    contract = _contract(tmp_path, backend="pi_rpc")

    assert runners._pi_selection(contract) == ("test-provider", "test-model")
    child_env = runners._pi_child_env(contract, hermes_root, "test-provider")
    assert child_env["PI_TEST_PROVIDER_KEY"] == "test-credential"
    assert "PI_TEST_UNRELATED" not in child_env


def test_pi_rpc_prompt_rejection_fails_immediately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _enable_pi_confinement(monkeypatch)
    pi_dir = tmp_path / "pi-agent"
    pi_dir.mkdir()
    (pi_dir / "settings.json").write_text(
        json.dumps({"defaultProvider": "test-provider", "defaultModel": "test-model"}),
        encoding="utf-8",
    )
    (pi_dir / "models.json").write_text(json.dumps({"providers": {}}), encoding="utf-8")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_dir))

    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "assert '--no-extensions' in sys.argv\n"
        "json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'id':'dispatch','type':'response','command':'prompt','success':False,'error':'provider unavailable'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)
    contract = _contract(tmp_path, backend="pi_rpc")
    contract["authorization"] = {"class": "read_only", "approved": True}

    with pytest.raises(RuntimeError, match="Pi RPC prompt failed: provider unavailable"):
        runners._worker_pi(str(fake_pi), contract, 5, tmp_path / "events.jsonl", tmp_path / "hermes")


def test_pi_stderr_burst_cannot_stall_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _enable_pi_confinement(monkeypatch)
    pi_dir = tmp_path / "pi-agent"
    pi_dir.mkdir()
    (pi_dir / "settings.json").write_text(json.dumps({}), encoding="utf-8")
    (pi_dir / "models.json").write_text(json.dumps({"providers": {}}), encoding="utf-8")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_dir))

    fake_pi = tmp_path / "fake-pi-noisy-stderr"
    fake_pi.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "json.loads(sys.stdin.readline())\n"
        "os.write(sys.stderr.fileno(), b'x' * (2 * 1024 * 1024))\n"
        "print(json.dumps({'type':'message_end','message':{'role':'assistant','content':'done'}}), flush=True)\n"
        "print(json.dumps({'type':'agent_settled','success':True}), flush=True)\n",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)
    contract = _contract(tmp_path, backend="pi_rpc")
    contract["authorization"] = {"class": "read_only", "approved": True}
    started = time.monotonic()
    rc, final_text = runners._worker_pi(str(fake_pi), contract, 5, tmp_path / "events.jsonl", tmp_path / "hermes")
    assert rc == 0
    assert final_text == "done"
    assert time.monotonic() - started < 5


def test_opencode_dry_run_uses_pure_json_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HERMES_GPT_OPENCODE_REMOTE_ENABLED", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    _enable_pi_confinement(monkeypatch)
    backend = runners.get_backend("opencode")
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")
    raw = _contract(
        ws,
        backend="opencode",
        options={"model": "cliproxyapi/glm-test", "agent": "build", "variant": "high"},
    )
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=root))
    assert payload["success"] is True
    assert payload["backend"] == "opencode"
    assert payload["plan"]["mode"] == "run"
    assert payload["plan"]["format"] == "json"
    assert payload["plan"]["pure"] is True
    assert payload["plan"]["sandbox"] == "workspace-write"
    assert payload["plan"]["model"] == "cliproxyapi/glm-test"


def test_opencode_worker_pipes_prompt_and_uses_confinement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        runners.confinement,
        "confinement_available",
        lambda *, writable=True, expose_proc=False: True,
    )

    def wrap(argv, workspace, *, writable=True, expose_proc=False):
        captured["wrapped"] = (list(argv), workspace, writable, expose_proc)
        return list(argv)

    monkeypatch.setattr(runners.confinement, "wrap_argv", wrap)
    real_key = "trusted-parent-only-value"
    material = {
        "model": "cliproxyapi/glm-test",
        "provider_id": "cliproxyapi",
        "model_id": "glm-test",
        "provider_name": "CLIProxyAPI",
        "npm": "@ai-sdk/openai-compatible",
        "timeout_ms": 60_000,
        "model_meta": {"name": "GLM test"},
        "upstream": runners.urllib.parse.urlparse("http://127.0.0.1:9/v1"),
        "real_key": real_key,
    }
    monkeypatch.setattr(runners, "_opencode_runtime_material", lambda *args, **kwargs: material)
    fake = tmp_path / "fake-opencode"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "prompt = sys.stdin.read()\n"
        "config = os.environ['OPENCODE_CONFIG_CONTENT']\n"
        "assert 'trusted-parent-only-value' not in config\n"
        "assert 'trusted-parent-only-value' not in repr(dict(os.environ))\n"
        "cfg = json.loads(config)\n"
        "relay_value = cfg['provider']['cliproxyapi']['options']['apiKey']\n"
        "assert relay_value and relay_value != 'trusted-parent-only-value'\n"
        "assert len(relay_value) >= 32\n"
        "assert prompt == 'Inspect the workspace and make the requested bounded change.'\n"
        "assert '--pure' in sys.argv and '--format' in sys.argv and 'json' in sys.argv\n"
        "assert prompt not in sys.argv\n"
        "print(json.dumps({'type':'text','part':{'type':'text','text':'done'}}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    contract = _contract(ws, backend="opencode", options={"model": "cliproxyapi/glm-test"})
    rc, final_text = runners._worker_opencode(str(fake), contract, 5, tmp_path / "events.jsonl")
    assert rc == 0
    assert final_text == "done"
    argv, wrapped_ws, writable, expose_proc = captured["wrapped"]
    assert wrapped_ws == ws.resolve()
    assert writable is True
    assert expose_proc is True
    assert "--auto" not in argv


def test_opencode_relay_replaces_child_authorization_without_serializing_parent_value():
    observed: dict[str, object] = {}

    class UpstreamHandler(runners.BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            observed["authorization"] = self.headers.get("Authorization")
            observed["path"] = self.path
            observed["body"] = self.rfile.read(length)
            payload = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    upstream = runners.ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = runners.threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    parent_value = "trusted-parent-only-value"
    material = {
        "model": "cliproxyapi/glm-test",
        "provider_id": "cliproxyapi",
        "model_id": "glm-test",
        "provider_name": "CLIProxyAPI",
        "npm": "@ai-sdk/openai-compatible",
        "timeout_ms": 60_000,
        "model_meta": {},
        "upstream": runners.urllib.parse.urlparse(f"http://127.0.0.1:{upstream.server_port}/v1"),
        "real_key": parent_value,
    }
    relay = runners._OpenCodeCredentialProxy(material)
    relay_thread = runners.threading.Thread(target=relay.serve_forever, daemon=True)
    relay_thread.start()
    try:
        child_config_text = runners._opencode_child_config(relay.material, relay.server_port)
        child_config = json.loads(child_config_text)
        relay_value = child_config["provider"]["cliproxyapi"]["options"]["apiKey"]
        assert parent_value not in child_config_text
        assert relay_value == relay.material["relay_token"]
        assert relay_value != parent_value

        for supplied in (None, "wrong-local-capability"):
            connection = runners.http.client.HTTPConnection("127.0.0.1", relay.server_port, timeout=5)
            headers = {"Content-Type": "application/json"}
            if supplied is not None:
                headers["Authorization"] = f"Bearer {supplied}"
            connection.request("POST", "/v1/chat/completions", body=b"{}", headers=headers)
            response = connection.getresponse()
            assert response.status == 401
            response.read()
            connection.close()
        assert observed == {}

        connection = runners.http.client.HTTPConnection("127.0.0.1", relay.server_port, timeout=5)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=b"{}",
            headers={
                "Authorization": f"Bearer {relay_value}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == b'{"ok":true}'
        connection.close()
        assert observed["authorization"] == f"Bearer {parent_value}"
        assert observed["path"] == "/v1/chat/completions"
        assert observed["body"] == b"{}"
    finally:
        relay.shutdown()
        relay.server_close()
        relay_thread.join(timeout=2)
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)


def test_read_only_opencode_uses_read_only_confinement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HERMES_GPT_OPENCODE_REMOTE_ENABLED", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    calls: list[tuple[bool, bool]] = []
    monkeypatch.setattr(
        runners.confinement,
        "confinement_available",
        lambda *, writable=True, expose_proc=False: calls.append((writable, expose_proc)) or True,
    )
    backend = runners.get_backend("opencode")
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")
    raw = _contract(ws, backend="opencode")
    raw["authorization"] = {"class": "read_only", "approved": True}
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=root))
    assert payload["success"] is True
    assert payload["plan"]["sandbox"] == "read-only"
    assert calls == [(False, True)]



def _setup_remote_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "opencode-workspaces"
    root.mkdir()
    (root / runners.OPENCODE_REMOTE_MARKER_NAME).write_text(
        runners.OPENCODE_REMOTE_MARKER_VALUE + "\n", encoding="utf-8"
    )
    workspace = root / "project"
    workspace.mkdir()
    monkeypatch.setenv(runners.OPENCODE_REMOTE_ENABLE_ENV, "1")
    monkeypatch.setenv("HERMES_GPT_OPENCODE_REMOTE_HARDENED", "1")
    monkeypatch.setenv(runners.OPENCODE_REMOTE_PASSWORD_ENV, "test-only-opencode-password")
    monkeypatch.setenv(runners.OPENCODE_REMOTE_USERNAME_ENV, "test-opencode-user")
    monkeypatch.delenv(runners.RUNNER_BACKEND_ALLOWLIST_ENV, raising=False)
    monkeypatch.delenv(runners.RUNNER_PROVIDER_ALLOWLIST_ENV, raising=False)
    monkeypatch.delenv(runners.RUNNER_MODEL_ALLOWLIST_ENV, raising=False)
    monkeypatch.setattr(runners, "_opencode_remote_workspace_root", lambda: root)
    monkeypatch.setattr(runners, "_opencode_remote_request", _remote_request_stub())
    return root, workspace


def test_remote_opencode_preflight_requires_authenticated_verified_readonly_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, _workspace = _setup_remote_root(tmp_path, monkeypatch)
    request = _remote_request_stub()
    paths: list[str] = []
    monkeypatch.setattr(
        runners,
        "_opencode_remote_request",
        lambda method, path, **kwargs: paths.append(path) or request(method, path, **kwargs),
    )
    result = runners._opencode_remote_preflight(root=root)
    assert result["agent"]["name"] == "hermes-readonly"
    assert result["root"] == root
    assert runners._opencode_remote_rules_safe(result["agent"]["permission"], scope="/workspaces")
    assert any(path.startswith("/config?") for path in paths)
    assert any(path.startswith("/agent?") for path in paths)

    monkeypatch.setattr(
        runners,
        "_opencode_remote_request",
        lambda method, path, **kwargs: {"default_agent": "build"} if path.startswith("/config?") else request(method, path, **kwargs),
    )
    with pytest.raises(runners._OpenCodeRemoteError, match="default agent/model is not safely configured"):
        runners._opencode_remote_preflight(root=root)

    monkeypatch.setattr(
        runners,
        "_opencode_remote_request",
        lambda method, path, **kwargs: (
            {"default_agent": runners.OPENCODE_REMOTE_AGENT, "model": "other-provider/other-model"}
            if path.startswith("/config?")
            else _remote_request_stub()(method, path, **kwargs)
        ),
    )
    with pytest.raises(runners._OpenCodeRemoteError, match="default agent/model is not safely configured"):
        runners._opencode_remote_preflight(root=root)

    wrong_agent = _readonly_remote_agent()
    wrong_agent["model"]["modelID"] = "other-model"
    monkeypatch.setattr(runners, "_opencode_remote_request", _remote_request_stub(agent=wrong_agent))
    with pytest.raises(runners._OpenCodeRemoteError, match="agent is not safely configured"):
        runners._opencode_remote_preflight(root=root)

    unsafe_agent = _readonly_remote_agent()
    unsafe_agent["permission"] = [
        *unsafe_agent["permission"],
        {"permission": "bash", "pattern": "*", "action": "allow"},
    ]
    monkeypatch.setattr(runners, "_opencode_remote_request", _remote_request_stub(agent=unsafe_agent))
    with pytest.raises(runners._OpenCodeRemoteError, match="agent is not safely configured"):
        runners._opencode_remote_preflight(root=root)


def test_remote_opencode_preflight_accepts_marker_with_or_without_final_newline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, _workspace = _setup_remote_root(tmp_path, monkeypatch)
    original_request = _remote_request_stub()

    def request_with_marker(content: str):
        def request(method: str, path: str, **kwargs):
            result = original_request(method, path, **kwargs)
            if path.startswith("/file/content?"):
                return {"type": "text", "content": content}
            return result
        return request

    for content in (runners.OPENCODE_REMOTE_MARKER_VALUE, runners.OPENCODE_REMOTE_MARKER_VALUE + "\n"):
        monkeypatch.setattr(runners, "_opencode_remote_request", request_with_marker(content))
        assert runners._opencode_remote_preflight(root=root)["root"] == root

    monkeypatch.setattr(
        runners,
        "_opencode_remote_request",
        request_with_marker(runners.OPENCODE_REMOTE_MARKER_VALUE + "-different"),
    )
    with pytest.raises(runners._OpenCodeRemoteError, match="workspace mapping is not verified"):
        runners._opencode_remote_preflight(root=root)


def test_remote_opencode_mount_root_does_not_replace_exact_contract_worktree_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, _workspace = _setup_remote_root(tmp_path, monkeypatch)
    original_request = _remote_request_stub()

    def root_reports_filesystem_worktree(method: str, path: str, **kwargs):
        result = original_request(method, path, **kwargs)
        if path.startswith("/path?"):
            query = runners.urllib.parse.parse_qs(runners.urllib.parse.urlsplit(path).query)
            directory = query["directory"][0]
            if directory == runners.OPENCODE_REMOTE_WORKSPACE_PATH:
                return {"directory": directory, "worktree": "/"}
            return {"directory": directory, "worktree": runners.OPENCODE_REMOTE_WORKSPACE_PATH}
        return result

    monkeypatch.setattr(runners, "_opencode_remote_request", root_reports_filesystem_worktree)
    assert runners._opencode_remote_preflight(root=root)["root"] == root

    with pytest.raises(runners._OpenCodeRemoteError, match="project root is wider"):
        runners._opencode_remote_verify_directory("/workspaces/project")

    def wrong_mount_directory(method: str, path: str, **kwargs):
        result = original_request(method, path, **kwargs)
        if path.startswith("/path?"):
            query = runners.urllib.parse.parse_qs(runners.urllib.parse.urlsplit(path).query)
            if query["directory"][0] == runners.OPENCODE_REMOTE_WORKSPACE_PATH:
                return {"directory": "/workspaces-other", "worktree": "/"}
        return result

    monkeypatch.setattr(runners, "_opencode_remote_request", wrong_mount_directory)
    with pytest.raises(runners._OpenCodeRemoteError, match="mount root is not verified"):
        runners._opencode_remote_verify_mount_root(runners.OPENCODE_REMOTE_WORKSPACE_PATH)


def test_remote_opencode_preflight_accepts_gateway_rw_root_but_requires_auth_and_hardening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, _workspace = _setup_remote_root(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_GPT_OPENCODE_REMOTE_HARDENED")
    with pytest.raises(runners._OpenCodeRemoteError, match="hardening has not been acknowledged"):
        runners._opencode_remote_preflight(root=root)

    monkeypatch.setenv("HERMES_GPT_OPENCODE_REMOTE_HARDENED", "1")
    gateway_write = root / ".gateway-rw-check"
    gateway_write.write_text("gateway mount is writable", encoding="utf-8")
    gateway_write.unlink()
    assert runners._opencode_remote_preflight(root=root)["root"] == root

    monkeypatch.delenv(runners.OPENCODE_REMOTE_PASSWORD_ENV)
    with pytest.raises(runners._OpenCodeRemoteError, match="Basic authentication is unavailable"):
        runners._opencode_remote_preflight(root=root)

    monkeypatch.setenv(runners.OPENCODE_REMOTE_PASSWORD_ENV, "test-only-opencode-password")
    monkeypatch.setattr(
        runners,
        "_opencode_remote_request",
        lambda method, path, **kwargs: {"healthy": True, "version": runners.OPENCODE_REMOTE_SUPPORTED_VERSION},
    )
    with pytest.raises(runners._OpenCodeRemoteError, match="Basic authentication is not enforced"):
        runners._opencode_remote_preflight(root=root)


def test_remote_opencode_workspace_rejects_symlinks_secrets_and_escapes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, workspace = _setup_remote_root(tmp_path, monkeypatch)
    sibling = root / "sibling"
    sibling.mkdir()
    link = root / "alias"
    link.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(runners._OpenCodeRemoteError, match="workspace root is not safely accessible"):
        runners._opencode_remote_preflight(root=root)
    link.unlink()
    (workspace / ".env").write_text("test-only=secret", encoding="utf-8")
    with pytest.raises(runners._OpenCodeRemoteError, match="denied secret path"):
        runners._opencode_remote_preflight(root=root)
    (workspace / ".env").unlink()

    outside = tmp_path / "outside"
    outside.mkdir()
    contract = _remote_readonly_contract(outside)
    with pytest.raises(runners._OpenCodeRemoteError, match="outside its verified shared root"):
        runners._opencode_remote_workspace(contract, root)
    assert runners._opencode_remote_workspace(_remote_readonly_contract(workspace), root) == (
        workspace, "/workspaces/project"
    )


def test_remote_opencode_availability_and_plan_do_not_require_local_bwrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, workspace = _setup_remote_root(tmp_path, monkeypatch)
    monkeypatch.setattr(runners, "_opencode_remote_preflight", lambda: {"agent": _readonly_remote_agent(), "root": root})
    monkeypatch.setattr(runners, "_opencode_remote_verify_directory", lambda path: {"directory": path, "worktree": path})
    backend = runners.get_backend("opencode")
    monkeypatch.setattr(backend, "executable", lambda: "/fake/opencode")
    monkeypatch.setattr(
        runners.confinement,
        "confinement_available",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("remote API client must not require local bwrap")),
    )
    assert backend.availability() == {"available": True, "remote": True, "executable": "/fake/opencode"}
    plan = backend.build_plan(_remote_readonly_contract(workspace))
    assert plan["mode"] == "remote-attach"
    assert plan["agent"] == "hermes-readonly"
    assert plan["workspace"] == "/workspaces/project"

    for auth_class in ("reversible_write", "high_impact"):
        contract = _remote_readonly_contract(workspace)
        contract["authorization"]["class"] = auth_class
        with pytest.raises(PermissionError, match="only approved read_only"):
            backend.build_plan(contract)



def test_remote_opencode_pin_does_not_narrow_global_allowlists_for_other_backends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, workspace = _setup_remote_root(tmp_path, monkeypatch)
    monkeypatch.setattr(runners, "_opencode_remote_preflight", lambda: {"agent": _readonly_remote_agent(), "root": root})
    backend = runners.get_backend("opencode")
    monkeypatch.setattr(backend, "executable", lambda: "/fake/opencode")
    result = backend.availability()
    assert result["available"] is True
    assert result["remote"] is True
    assert runners.RUNNER_BACKEND_ALLOWLIST_ENV not in os.environ
    assert runners.RUNNER_PROVIDER_ALLOWLIST_ENV not in os.environ
    assert runners.RUNNER_MODEL_ALLOWLIST_ENV not in os.environ
    assert backend.build_plan(_remote_readonly_contract(workspace))["model"] == "hermes-proxy/openai/gpt-6-luna"

    monkeypatch.setattr(runners.confinement, "confinement_available", lambda **_kwargs: True)
    pi_contract = _remote_readonly_contract(workspace, options={"provider": "other-provider", "model": "other-model"})
    pi_plan = runners.PiRpcBackend().build_plan(pi_contract)
    assert pi_plan["provider"] == "other-provider" and pi_plan["model"] == "other-model"

    monkeypatch.setenv(runners.RUNNER_PROVIDER_ALLOWLIST_ENV, "hermes-proxy")
    monkeypatch.setenv(runners.RUNNER_MODEL_ALLOWLIST_ENV, "other-model")
    with pytest.raises(PermissionError, match="OpenCode model is not allowed"):
        backend.build_plan(_remote_readonly_contract(workspace))

    monkeypatch.setenv(runners.RUNNER_PROVIDER_ALLOWLIST_ENV, "other-provider")
    with pytest.raises(PermissionError, match="OpenCode provider is not allowed"):
        backend.build_plan(_remote_readonly_contract(workspace))


def test_remote_opencode_contract_audit_excludes_prompt_and_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, workspace = _setup_remote_root(tmp_path, monkeypatch)
    _enable_workspace(monkeypatch, workspace)
    monkeypatch.setenv(runners.RUNNER_BACKEND_ALLOWLIST_ENV, "opencode")
    monkeypatch.setattr(runners, "_opencode_remote_preflight", lambda: {"agent": _readonly_remote_agent(), "root": root})
    monkeypatch.setattr(runners, "_opencode_remote_verify_directory", lambda path: {"directory": path, "worktree": path})
    backend = runners.get_backend("opencode")
    monkeypatch.setattr(backend, "executable", lambda: "/fake/opencode")
    raw = _remote_readonly_contract(workspace)
    raw["task_id"] = "runner-remote-audit-001"
    raw["objective"] = "test-only prompt that must not be retained"
    records: list[dict] = []
    monkeypatch.setattr(contract_mod.op, "audit_record", lambda **record: records.append(record))
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=tmp_path / "audit-root"))
    encoded = json.dumps({"payload": payload, "audit": records})
    assert payload["success"] is True
    assert raw["objective"] not in encoded
    assert "test-only-opencode-password" not in encoded


def test_remote_opencode_deployment_templates_enforce_proxy_and_agent_boundaries():
    from deploy.opencode.verify_deployment import EXPECTED_MOUNTS, EXPECTED_PROXY_MOUNTS, _INSPECT_TEMPLATE

    deploy = Path(__file__).parent / "deploy" / "opencode"
    compose = (deploy / "docker-compose.yml").read_text(encoding="utf-8")
    nginx = (deploy / "nginx.conf").read_text(encoding="utf-8")
    agent = (deploy / "hermes-readonly.md").read_text(encoding="utf-8")
    server_config = json.loads((deploy / "opencode.default-agent.fragment.json").read_text(encoding="utf-8"))
    assert "/tmp" not in EXPECTED_MOUNTS and "/home/node/.config/opencode" not in EXPECTED_MOUNTS
    assert "/home/node/.config/opencode/agents" in EXPECTED_MOUNTS
    assert "/tmp" not in EXPECTED_PROXY_MOUNTS
    config_content = next(
        line.split(":", 1)[1].strip().strip("'")
        for line in compose.splitlines()
        if line.strip().startswith("OPENCODE_CONFIG_CONTENT:")
    )
    assert json.loads(config_content) == {
        "default_agent": server_config["default_agent"],
        "model": server_config["model"],
    }

    assert '"env_names"' in _INSPECT_TEMPLATE and "split $env \"=\"" in _INSPECT_TEMPLATE
    assert '"env_presence"' in _INSPECT_TEMPLATE and '"empty"' in _INSPECT_TEMPLATE
    assert "{{json .Config.Env}}" not in _INSPECT_TEMPLATE
    assert "json $env" not in _INSPECT_TEMPLATE and "json (index $parts 1)" not in _INSPECT_TEMPLATE

    assert 'image: node:22-bookworm' in compose and 'user: "1000:1000"' in compose
    assert 'network_mode: "service:opencode"' in compose
    assert "--hostname" in compose and "127.0.0.1" in compose and '"4096"' in compose
    assert "--pure" in compose and "--log-level" in compose and "ERROR" in compose
    assert "pids_limit: 256" in compose and "mem_limit: 2g" in compose and "cpus: 1.0" in compose
    assert 'hermes-opencode-app:/opt/opencode:ro' in compose
    assert 'hermes-opencode-state:/home/node/.local' in compose
    assert '${OPENCODE_CONFIG_FILE:?set OPENCODE_CONFIG_FILE}:/opencode.json:ro' in compose
    assert '${OPENCODE_WORKSPACE_SOURCE:?set OPENCODE_WORKSPACE_SOURCE}:/workspaces:ro' in compose
    assert '${OPENCODE_AGENT_CONFIG_DIR:?set OPENCODE_AGENT_CONFIG_DIR}:/home/node/.config/opencode/agents:ro' in compose
    assert '- /home/node/.config/opencode:rw,noexec,nosuid,nodev,size=16m,uid=1000,gid=1000' in compose
    assert "OPENCODE_SERVER_PASSWORD" not in compose and "API_KEY" not in compose
    for setting in (
        "HOME: /home/node",
        "XDG_CONFIG_HOME: /home/node/.config",
        "XDG_DATA_HOME: /home/node/.local/share",
        "XDG_CACHE_HOME: /tmp/cache",
        'OPENCODE_DISABLE_AUTOUPDATE: "1"',
        "OPENCODE_CONFIG: /opencode.json",
        "OPENCODE_CONFIG_DIR: /home/node/.config/opencode",
    ):
        assert setting in compose
    assert "env_file:" not in compose
    assert "read_only: true" in compose and "no-new-privileges:true" in compose and "cap_drop:" in compose
    assert server_config == {
        "$schema": "https://opencode.ai/config.json",
        "default_agent": "hermes-readonly",
        "model": "hermes-proxy/openai/gpt-6-luna",
    }
    assert server_config.get("provider") is None
    assert "ports:" not in compose
    assert "listen 4097;" in nginx and "proxy_pass http://127.0.0.1:4096;" in nginx
    assert "proxy_set_header Authorization \"\";" in nginx
    assert "proxy_set_header Proxy-Authorization \"\";" in nginx
    assert "access_log off;" in nginx and "auth_basic_user_file /run/secrets/opencode.htpasswd;" in nginx
    assert "OPENCODE_SERVER_PASSWORD" not in compose + nginx + agent
    for allowed in ("read:", "glob:", "grep:", "list:"):
        assert allowed in agent
    for denied in ("bash: deny", "edit: deny", "write: deny", "patch: deny", "webfetch: deny", "task: deny", "external_directory: deny", '\"/proc/**\": deny'):
        assert denied in agent


def test_webui_deployment_verifier_requires_derived_password_and_rejects_other_auth():
    from deploy.opencode.verify_deployment import _webui_env_errors

    def env_container(values: dict[str, bool]) -> dict:
        return {
            "config": {
                "env_names": [*values, "PATH"],
                "env_presence": [
                    *({"name": name, "empty": empty} for name, empty in values.items()),
                    {"name": "PATH", "empty": False},
                ],
            }
        }

    webui = env_container({"HERMES_WEBUI_PASSWORD": False, "SERVICE_PASSWORD_HERMESWEBUI": True})
    assert _webui_env_errors(webui) == []

    webui["config"]["env_presence"][0]["empty"] = True
    assert any("HERMES_WEBUI_PASSWORD must be present and nonempty" in error for error in _webui_env_errors(webui))
    webui["config"]["env_presence"][0]["empty"] = False

    webui["config"]["env_names"].remove("HERMES_WEBUI_PASSWORD")
    webui["config"]["env_presence"].pop(0)
    assert any("HERMES_WEBUI_PASSWORD must be present and nonempty" in error for error in _webui_env_errors(webui))

    webui["config"]["env_names"].append("HERMES_WEBUI_PASSWORD")
    webui["config"]["env_presence"].insert(0, {"name": "HERMES_WEBUI_PASSWORD", "empty": False})
    webui["config"]["env_presence"][1]["empty"] = False
    assert any("SERVICE_PASSWORD_HERMESWEBUI must be absent or empty" in error for error in _webui_env_errors(webui))

    webui["config"]["env_names"].append("OPENCODE_SERVER_PASSWORD")
    webui["config"]["env_presence"].append({"name": "OPENCODE_SERVER_PASSWORD", "empty": False})
    assert any("contain OpenCode remote auth" in error for error in _webui_env_errors(webui))



def test_deployment_preflight_attests_remote_ro_mount_and_env_placement(tmp_path: Path):
    from deploy.opencode.verify_deployment import SERVER_COMMAND, validate_deployment

    data_source = tmp_path / "gateway-data"
    workspace_source = data_source / "opencode-workspaces"
    workspace_source.mkdir(parents=True)
    (workspace_source / "gateway-write-check").write_text("gateway side is writable", encoding="utf-8")
    config_file = tmp_path / "opencode.json"
    config_file.write_text("{}", encoding="utf-8")
    agent_config_dir = tmp_path / "agent-config"
    agent_config_dir.mkdir()
    (agent_config_dir / "hermes-readonly.md").write_text("test profile", encoding="utf-8")

    def env_config(names: list[str], *, nonempty: tuple[str, ...] = ()) -> dict:
        return {
            "env_names": names,
            "env_presence": [
                {"name": name, "empty": name not in nonempty}
                for name in names
            ],
        }

    server_id = "a" * 64
    server = {
        "id": server_id,
        "running": True,
        "config": {
            "image": "node:22-bookworm",
            "user": "1000:1000",
            "cmd": SERVER_COMMAND,
            **env_config(["HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"]),
        },
        "host": {
            "readonly_rootfs": True, "cap_drop": ["ALL"], "cap_add": [],
            "security_opt": ["no-new-privileges:true"], "memory": 2 * 1024**3,
            "nano_cpus": 1_000_000_000, "pids_limit": 256,
            "restart": {"Name": "unless-stopped"}, "port_bindings": None,
            "tmpfs": {
                "/tmp": "rw,noexec,nosuid,nodev,size=64m,uid=1000,gid=1000",
                "/home/node/.config/opencode": "rw,noexec,nosuid,nodev,size=16m,uid=1000,gid=1000",
            },
        },
        "networks": {"hermes-opencode-int": {}},
        "ports": {"4096/tcp": None},
        "mounts": [
            {"Type": "volume", "Name": "hermes-opencode-app", "Destination": "/opt/opencode", "RW": False},
            {"Type": "volume", "Name": "hermes-opencode-state", "Destination": "/home/node/.local", "RW": True},
            {"Type": "bind", "Source": str(config_file), "Destination": "/opencode.json", "RW": False},
            {"Type": "bind", "Source": str(workspace_source), "Destination": "/workspaces", "RW": False},
            {"Type": "bind", "Source": str(agent_config_dir), "Destination": "/home/node/.config/opencode/agents", "RW": False},
        ],
    }
    proxy = {
        "id": "b" * 64,
        "running": True,
        "config": {"user": "101:101", **env_config(["PATH", "NGINX_VERSION"])},
        "host": {
            "readonly_rootfs": True, "cap_drop": ["ALL"], "cap_add": [],
            "security_opt": ["no-new-privileges:true"], "memory": 128 * 1024**2,
            "nano_cpus": 250_000_000, "pids_limit": 32,
            "network_mode": f"container:{server_id}", "port_bindings": None,
            "tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=101,gid=101"},
        },
        "ports": None,
        "mounts": [
            {"Type": "bind", "Source": str(tmp_path / "nginx.conf"), "Destination": "/etc/nginx/nginx.conf", "RW": False},
            {"Type": "bind", "Source": str(tmp_path / "htpasswd"), "Destination": "/run/secrets/opencode.htpasswd", "RW": False},
        ],
    }
    gateway = {
        "config": env_config(
            ["HERMES_GPT_OPENCODE_SERVER_PASSWORD", "HERMES_WEBUI_PASSWORD", "SERVICE_PASSWORD_HERMESWEBUI", "PATH"],
            nonempty=("HERMES_GPT_OPENCODE_SERVER_PASSWORD",),
        ),
        "networks": {"hermes-opencode-int": {}},
        "mounts": [{"Type": "bind", "Source": str(data_source), "Destination": "/opt/data", "RW": True}],
    }
    agent = {"config": env_config(["PATH", "HERMES_HOME", "HERMES_WEBUI_PASSWORD", "SERVICE_PASSWORD_HERMESWEBUI"])}
    webui = {
        "config": env_config(
            ["PATH", "WEBUI_PORT", "HERMES_WEBUI_PASSWORD", "SERVICE_PASSWORD_HERMESWEBUI"],
            nonempty=("HERMES_WEBUI_PASSWORD",),
        )
    }

    def verify():
        validate_deployment(
            server, proxy, gateway, agent, webui,
            network_internal=True,
            workspace_source=workspace_source,
            config_file=config_file,
            agent_config_dir=agent_config_dir,
            server_proc_secure=True,
            proxy_proc_secure=True,
        )

    verify()
    agent_mount = server["mounts"][4]
    agent_mount["Destination"] = "/opt/opencode-config"
    with pytest.raises(ValueError, match="OpenCode server mounts are not limited"):
        verify()
    agent_mount["Destination"] = "/home/node/.config/opencode/agents"
    agent_mount["RW"] = True
    with pytest.raises(ValueError, match="OpenCode config mount /home/node/.config/opencode/agents is not the expected read-only source"):
        verify()
    agent_mount["RW"] = False

    webui_password = next(
        item for item in webui["config"]["env_presence"] if item["name"] == "HERMES_WEBUI_PASSWORD"
    )
    webui_password["empty"] = True
    with pytest.raises(ValueError, match="WebUI HERMES_WEBUI_PASSWORD must be present and nonempty"):
        verify()
    webui_password["empty"] = False
    assert (workspace_source / "gateway-write-check").is_file()
    server["mounts"][3]["RW"] = True
    with pytest.raises(ValueError, match="OpenCode workspace mount is not the expected read-only dedicated source"):
        verify()
    server["mounts"][3]["RW"] = False

    gateway["config"]["env_names"].remove("HERMES_GPT_OPENCODE_SERVER_PASSWORD")
    with pytest.raises(ValueError, match="Gateway does not expose the remote Basic password variable name"):
        verify()
    gateway["config"]["env_names"].append("HERMES_GPT_OPENCODE_SERVER_PASSWORD")

    for target, message in (
        (gateway, "Gateway WebUI password variables must be absent or empty"),
        (agent, "Agent WebUI password variables must be absent or empty"),
    ):
        for variable in ("HERMES_WEBUI_PASSWORD", "SERVICE_PASSWORD_HERMESWEBUI"):
            entry = next(item for item in target["config"]["env_presence"] if item["name"] == variable)
            entry["empty"] = False
            with pytest.raises(ValueError, match=message):
                verify()
            entry["empty"] = True

    webui_service_password = next(
        item for item in webui["config"]["env_presence"] if item["name"] == "SERVICE_PASSWORD_HERMESWEBUI"
    )
    webui_service_password["empty"] = False
    with pytest.raises(ValueError, match="WebUI SERVICE_PASSWORD_HERMESWEBUI must be absent or empty"):
        verify()
    webui_service_password["empty"] = True
    verify()

    for target in (agent, webui, server, proxy):
        target["config"].setdefault("env_names", []).append("HERMES_GPT_OPENCODE_SERVER_PASSWORD")
        with pytest.raises(ValueError, match="remote auth|secret-bearing"):
            verify()
        target["config"]["env_names"].pop()

    proxy["host"]["cap_add"] = ["SYS_ADMIN"]
    with pytest.raises(ValueError, match="auth proxy rootfs, capabilities"):
        verify()

    proxy["host"]["cap_add"] = []
    proxy["host"]["security_opt"] = ["no-new-privileges:true", "seccomp=unconfined"]
    with pytest.raises(ValueError, match="auth proxy rootfs, capabilities"):
        verify()

    from deploy.opencode.verify_deployment import _parse_proc_status

    assert _parse_proc_status("Name:\topencode\nSeccomp:\t2\nNoNewPrivs:\t1\n")
    assert not _parse_proc_status("Seccomp:\t0\nNoNewPrivs:\t1\n")


def test_remote_opencode_final_assistant_result_is_bounded_job_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from operator_job_supervisor import get_job, register_job

    hermes_root = tmp_path / "hermes"
    jobs_root = hermes_root / "runner-jobs"
    jobs_root.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = "task-opencode-result"
    meta_path = jobs_root / f"{task_id}.json"
    request_path = jobs_root / f"{task_id}.request.json"
    log_path = jobs_root / f"{task_id}.jsonl"
    objective = "private objective for bounded result test"
    contract = _remote_readonly_contract(workspace)
    contract.update({"task_id": task_id, "objective": objective})
    meta_path.write_text(json.dumps({"task_id": task_id, "backend": "opencode", "state": "queued", "remote_mode": True}), encoding="utf-8")
    request_path.write_text(json.dumps({
        "backend": "opencode", "contract": contract, "timeout": 20,
        "hermes_root": str(hermes_root), "remote_mode": True,
    }), encoding="utf-8")
    register_job(
        task_id, backend="opencode", workspace=workspace, log_path=log_path,
        source_record=meta_path, cancel_path=jobs_root / f"{task_id}.cancel.json",
        hermes_root=hermes_root,
    )
    backend = runners.OpenCodeBackend()
    monkeypatch.setattr(backend, "executable", lambda: "/fake/opencode")
    monkeypatch.setattr(runners, "get_backend", lambda _name: backend)
    monkeypatch.setattr(runners, "_opencode_remote_enabled", lambda: True)
    raw_result = "Remote assistant result marker" + ("x" * (runners._MAX_RESULT_CHARS + 100))
    monkeypatch.setattr(runners, "_worker_opencode_remote", lambda *_args: (0, raw_result))

    assert runners._worker(task_id, jobs_root) == 0
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    observed = backend.observed_runs(task_id, hermes_root=hermes_root)[0]
    job = get_job(task_id, hermes_root=hermes_root)
    assert meta["remote_result"].startswith("Remote assistant result marker")
    assert len(meta["remote_result"]) == runners._MAX_RESULT_CHARS
    assert observed["result"] == meta["remote_result"]
    assert job["result_summary"].startswith("Remote assistant result marker")
    assert objective not in json.dumps(meta) + json.dumps(observed) + json.dumps(job)
    assert not request_path.exists()
    if log_path.exists():
        assert objective not in log_path.read_text(encoding="utf-8")

def test_remote_opencode_run_attach_env_session_and_redaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, workspace = _setup_remote_root(tmp_path, monkeypatch)
    objective = "Inspect the workspace; do not persist this prompt"
    contract = _remote_readonly_contract(workspace)
    contract["objective"] = objective
    monkeypatch.setattr(runners, "_opencode_remote_preflight", lambda: {"agent": _readonly_remote_agent(), "root": root})
    monkeypatch.setattr(runners, "_opencode_remote_verify_directory", lambda path: {"directory": path, "worktree": path})
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-provider-key")
    fake = tmp_path / "fake-opencode-remote"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "prompt = sys.stdin.read()\n"
        "assert prompt == " + repr(objective) + "\n"
        "assert 'run' in sys.argv and '--attach' in sys.argv and '--pure' in sys.argv\n"
        "assert 'http://hermes-opencode:4097' in sys.argv\n"
        "assert sys.argv[sys.argv.index('--dir') + 1] == '/workspaces/project'\n"
        "assert sys.argv[sys.argv.index('--agent') + 1] == 'hermes-readonly'\n"
        "assert '--password' not in sys.argv and 'test-only-opencode-password' not in ' '.join(sys.argv)\n"
        "assert os.environ['OPENCODE_SERVER_PASSWORD'] == 'test-only-opencode-password'\n"
        "assert os.environ['OPENCODE_SERVER_USERNAME'] == 'test-opencode-user'\n"
        "assert os.getcwd() == os.environ['HOME'] == os.environ['TMPDIR']\n"
        "assert 'OPENAI_API_KEY' not in os.environ\n"
        "assert 'HERMES_HOME' not in os.environ and 'PI_CODING_AGENT_DIR' not in os.environ\n"
        "print(json.dumps({'type':'text','sessionID':'sesRemoteTest01','part':{'type':'text','text':'Workspace marker verified: hermes-gpt-opencode-workspace-v1' + ('x' * "
        + str(runners._MAX_RESULT_CHARS + 50)
        + ")}}), flush=True)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    meta_path = tmp_path / "runner.json"
    meta_path.write_text("{}", encoding="utf-8")
    log_path = tmp_path / "runner.jsonl"
    rc, result = runners._worker_opencode_remote(str(fake), contract, 5, log_path, meta_path)
    expected_response = "Workspace marker verified: hermes-gpt-opencode-workspace-v1"
    assert rc == 0 and result.startswith(expected_response)
    assert len(result) == runners._MAX_RESULT_CHARS and result.endswith("...")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["remote_session_id"] == "sesRemoteTest01"
    assert meta["remote_directory"] == "/workspaces/project"
    logs = log_path.read_text(encoding="utf-8")
    metadata = meta_path.read_text(encoding="utf-8")
    assert objective not in logs and expected_response not in logs
    assert objective not in metadata and expected_response not in metadata
    assert "test-only-opencode-password" not in logs + metadata
    assert "sesRemoteTest01" in logs


def test_remote_opencode_resume_uses_validated_session_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, workspace = _setup_remote_root(tmp_path, monkeypatch)
    session_id = "sesResumeTest01"
    contract = _remote_readonly_contract(workspace, options={"session_id": session_id})
    monkeypatch.setattr(runners, "_opencode_remote_preflight", lambda: {"agent": _readonly_remote_agent(), "root": root})
    monkeypatch.setattr(runners, "_opencode_remote_verify_directory", lambda path: {"directory": path, "worktree": path})
    monkeypatch.setattr(
        runners,
        "_opencode_remote_session",
        lambda received, path: {
            "id": received, "agent": "hermes-readonly", "directory": path,
            "model": {"providerID": "hermes-proxy", "modelID": "openai/gpt-6-luna"}, "permission": None,
        },
    )
    fake = tmp_path / "fake-opencode-resume"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "assert sys.argv[sys.argv.index('--session') + 1] == " + repr(session_id) + "\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'text','sessionID':" + repr(session_id) + "}), flush=True)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    meta_path = tmp_path / "resume.json"
    meta_path.write_text("{}", encoding="utf-8")
    rc, _ = runners._worker_opencode_remote(str(fake), contract, 5, tmp_path / "resume.jsonl", meta_path)
    assert rc == 0


def test_remote_opencode_timeout_aborts_known_session_without_logging_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, workspace = _setup_remote_root(tmp_path, monkeypatch)
    contract = _remote_readonly_contract(workspace)
    monkeypatch.setattr(runners, "_opencode_remote_preflight", lambda: {"agent": _readonly_remote_agent(), "root": root})
    monkeypatch.setattr(runners, "_opencode_remote_verify_directory", lambda path: {"directory": path, "worktree": path})
    aborted: list[tuple[str, str]] = []
    monkeypatch.setattr(runners, "_opencode_remote_abort", lambda session, path: aborted.append((session, path)))
    fake = tmp_path / "fake-opencode-timeout"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, time\n"
        "print(json.dumps({'type':'text','sessionID':'sesTimeoutTest01','text':'private'}), flush=True)\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    log_path = tmp_path / "timeout.jsonl"
    started = time.monotonic()
    rc, _ = runners._worker_opencode_remote(str(fake), contract, 1, log_path, tmp_path / "timeout.json")
    assert rc == 124
    assert aborted == [("sesTimeoutTest01", "/workspaces/project")]
    assert "private" not in log_path.read_text(encoding="utf-8")
    assert time.monotonic() - started < 6


def test_omx_timeout_kills_descendant_holding_inherited_pipes(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake_omx = tmp_path / "fake-omx-descendant"
    fake_omx.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', \"import sys,time; sys.stdout.write('held'); sys.stdout.flush(); time.sleep(60)\"])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    fake_omx.chmod(0o755)
    contract = _contract(ws, backend="omx")
    started = time.monotonic()
    rc, final_text = runners._worker_omx(str(fake_omx), contract, 1, tmp_path / "events.jsonl")
    assert rc == 124
    assert final_text == ""
    assert time.monotonic() - started < 8


def test_omx_dry_run_uses_native_exec_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    backend = runners.get_backend("omx")
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")
    raw = _contract(ws, backend="omx")
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=root))
    assert payload["success"] is True
    assert payload["backend"] == "omx"
    assert payload["plan"]["mode"] == "exec"
    assert payload["plan"]["sandbox"] == "workspace-write"


def test_runner_job_is_observed_by_contract_validator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    raw = _contract(ws, backend="pi_rpc")
    _, parsed, _ = contract_mod._parse_contract(json.dumps(raw))
    meta_path, _, _ = runners._job_paths(parsed["task_id"], root)
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION,
        "task_id": parsed["task_id"],
        "backend": "pi_rpc",
        "state": "completed",
        "outcome": "completed",
        "created_at": "2026-08-17T00:00:00+00:00",
        "started_at": "2026-08-17T00:00:01+00:00",
        "ended_at": "2026-08-17T00:00:02+00:00",
        "error": "",
    })
    check = contract_mod._check_run_state(parsed, root)
    assert check["status"] == "PASS"
    assert "runner:pi_rpc" in check["detail"]


def test_execution_options_reject_secret_like_keys(tmp_path: Path):
    raw = _contract(tmp_path, backend="pi_rpc", options={"api_key": "do-not-inline"})
    with pytest.raises(ValueError, match="must not carry secrets"):
        contract_mod._parse_contract(json.dumps(raw))


def test_canonical_swarm_accepts_per_stage_execution(tmp_path: Path):
    import operator_swarm as swarm
    import operator_swarm_workflows as workflows

    wf = workflows.canonical_workflow(
        title="Runner workflow",
        workspace=str(tmp_path),
        owners={"implementation": "coder", "codex_review": "reviewer"},
        executions={
            "implementation": {"backend": "pi_rpc", "options": {}},
            "codex_review": {"backend": "omx", "options": {"sandbox": "read-only"}},
        },
    )
    impl = next(stage for stage in wf["stages"] if stage["id"] == "implementation")
    review = next(stage for stage in wf["stages"] if stage["id"] == "codex_review")
    assert impl["execution"]["backend"] == "pi_rpc"
    assert review["execution"]["backend"] == "omx"
    assert review["review_requirements"]["reviewer"] == "reviewer"

    contract = swarm._stage_contract(wf, impl, task_id="runner-stage-001")
    _, parsed, _ = contract_mod._parse_contract(json.dumps(contract))
    assert parsed["execution"]["backend"] == "pi_rpc"


def test_read_only_pi_cannot_enable_write_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    backend = runners.get_backend("pi_rpc")
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")
    raw = _contract(ws, backend="pi_rpc", options={"tools": "read,bash,edit,write"})
    raw["authorization"] = {"class": "read_only", "approved": True}
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=root))
    assert payload["success"] is False
    assert payload["code"] == "RUNNER_DISPATCH_ERROR"
    assert "read tool" in payload["safe_message"]


def test_pi_writable_contract_rejected_until_filesystem_confinement_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    backend = runners.get_backend("pi_rpc")
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")
    raw = _contract(ws, backend="pi_rpc")
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=root))
    assert payload["success"] is False
    assert payload["code"] == "RUNNER_DISPATCH_ERROR"
    assert "filesystem confinement" in payload["safe_message"]


def test_read_only_omx_cannot_request_workspace_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    backend = runners.get_backend("omx")
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")
    raw = _contract(ws, backend="omx", options={"sandbox": "workspace-write"})
    raw["authorization"] = {"class": "read_only", "approved": True}
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=root))
    assert payload["success"] is False
    assert payload["code"] == "RUNNER_DISPATCH_ERROR"
    assert "read-only authorization" in payload["safe_message"]


def test_runner_cancel_enforces_job_workspace_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    allowed = tmp_path / "allowed"
    other = tmp_path / "other"
    root = tmp_path / "hermes"
    allowed.mkdir()
    other.mkdir()
    root.mkdir()
    _enable_workspace(monkeypatch, allowed)
    task_id = "runner-cancel-scope"
    meta_path, _, _ = runners._job_paths(task_id, root)
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION,
        "task_id": task_id,
        "backend": "pi_rpc",
        "state": "running",
        "workspace": str(other),
        "pid": None,
    })
    payload = json.loads(runners.hermes_runner_cancel(task_id, backend="pi_rpc", dry_run=True, hermes_root=root))
    assert payload["success"] is False
    assert payload["code"] == "RUNNER_CANCEL_ERROR"


def test_runner_cancel_refuses_unverified_legacy_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    root = tmp_path / "hermes"
    ws.mkdir()
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    task_id = "runner-cancel-tree"
    meta_path, _, _ = runners._job_paths(task_id, root)
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION,
        "task_id": task_id,
        "backend": "pi_rpc",
        "state": "running",
        "outcome": "running",
        "workspace": str(ws),
        "pid": 4242,
        "ended_at": None,
    })
    terminated = []
    monkeypatch.setattr(
        runners,
        "_terminate_process_tree",
        lambda target, timeout=5.0: terminated.append((target, timeout)),
    )

    payload = json.loads(
        runners.hermes_runner_cancel(
            task_id,
            backend="pi_rpc",
            confirm=True,
            dry_run=False,
            hermes_root=root,
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "JOB_PROCESS_UNVERIFIABLE"
    assert terminated == []
    stored = json.loads(meta_path.read_text(encoding="utf-8"))
    assert stored["state"] == "running"
    assert stored["outcome"] == "running"

def test_backend_cancel_uses_shared_supervisor_and_marks_source_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    task_id = "runner-cancel-supervised"
    meta_path = tmp_path / "job.json"
    request_path = tmp_path / "request.json"
    log_path = tmp_path / "events.jsonl"
    cancel_path = tmp_path / "cancel.json"
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION,
        "task_id": task_id,
        "backend": "pi_rpc",
        "state": "running",
        "outcome": "running",
        "workspace": str(tmp_path),
        "pid": 4343,
        "ended_at": None,
    })
    monkeypatch.setattr(
        runners,
        "_job_paths",
        lambda task_id, hermes_root=None: (meta_path, request_path, log_path),
    )
    monkeypatch.setattr(
        runners,
        "_cancel_path",
        lambda task_id, hermes_root=None: cancel_path,
    )
    calls = []
    monkeypatch.setattr(
        runners.job_supervisor,
        "request_cancel",
        lambda task_id, hermes_root=None: calls.append((task_id, hermes_root)) or {
            "success": True,
            "changed": True,
            "status": "cancelled",
        },
    )

    result = runners.PiRpcBackend().cancel(task_id, hermes_root=tmp_path)

    assert result["success"] is True
    assert result["state"] == "cancelled"
    assert calls == [(task_id, tmp_path)]
    stored = json.loads(meta_path.read_text(encoding="utf-8"))
    assert stored["state"] == "cancelled"
    assert stored["outcome"] == "cancelled"
    assert stored["ended_at"]

# ---------------------------------------------------------------------------
# PR #18 correctness regression tests
# ---------------------------------------------------------------------------


def test_popen_failure_deletes_request_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Spawn failure after envelope writes must delete the request (raw
    objective) and leave only bounded failed metadata."""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    _enable_pi_confinement(monkeypatch)
    backend = runners.get_backend("pi_rpc")
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")

    def _boom(*args, **kwargs):
        raise RuntimeError("spawn refused")

    monkeypatch.setattr(runners.subprocess, "Popen", _boom)
    raw = _contract(ws, backend="pi_rpc")
    raw["authorization"] = {"class": "read_only", "approved": True}
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=False, confirm=True, hermes_root=root))
    assert payload["success"] is False
    assert payload["code"] == "RUNNER_SPAWN_FAILED"
    meta_path, request_path, _ = runners._job_paths(raw["task_id"], root)
    assert not request_path.exists()
    meta = json.loads(meta_path.read_text())
    assert meta["state"] == "failed"
    assert meta["outcome"] == "failed"
    assert raw["objective"] not in json.dumps(meta)


def test_fleet_exception_keeps_legacy_contract_dispatch_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    fleet = runners.get_backend("fleet")
    monkeypatch.setattr(fleet, "dispatch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fleet peer unreachable")))
    raw = _contract(ws)  # no execution selector -> implicit fleet
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=root))
    assert payload["success"] is False
    assert payload["code"] == "CONTRACT_DISPATCH_ERROR"
    assert payload["suggested_action"] == "Check fleet authority manifest, registry, and peer service."


def test_explicit_fleet_selector_uses_runner_dispatch_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    fleet = runners.get_backend("fleet")
    monkeypatch.setattr(fleet, "dispatch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    raw = _contract(ws, backend="fleet")
    payload = runners.dispatch_contract(raw, confirm=False, dry_run=True, timeout=30, hermes_root=root)
    assert payload["code"] == "RUNNER_DISPATCH_ERROR"


def test_non_fleet_backend_exception_uses_runner_dispatch_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    codex = runners.get_backend("codex")
    monkeypatch.setattr(codex, "dispatch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("codex backend exploded")))
    raw = _contract(ws, backend="codex")
    payload = runners.dispatch_contract(raw, confirm=False, dry_run=True, timeout=30, hermes_root=root)
    assert payload["success"] is False
    assert payload["code"] == "RUNNER_DISPATCH_ERROR"
    assert payload["backend"] == "codex"


def test_codex_observed_runs_uses_normalized_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import operator_codex as op_codex

    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "env-hermes"))
    backend = runners.CodexBackend()
    # With hermes_root given, observed_runs must use the normalized root
    # (op_codex._root), not hermes_root/'codex-jobs' or ~/.hermes directly.
    normalized = op_codex._root(root)
    normalized.mkdir(parents=True, exist_ok=True)
    normalized.joinpath("job-1.json").write_text(json.dumps({
        "job_id": "job-1", "state": "completed", "outcome": "completed", "task_id": "codex-link-001",
    }), encoding="utf-8")
    runs = backend.observed_runs("codex-link-001", hermes_root=root)
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["scope"] == "runner:codex"
    # And normalization applies without an explicit hermes_root too.
    env_root = op_codex._root(None)
    if env_root != normalized:
        assert backend.observed_runs("codex-link-001") == []


def _fake_backend(monkeypatch):
    backend = runners.PiRpcBackend()
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")
    return backend


def _make_job(root: Path, task_id: str, *, backend: str = "pi_rpc", state: str = "running") -> Path:
    meta_path = root / f"{task_id}.json"
    request_path = root / f"{task_id}.request.json"
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION,
        "task_id": task_id,
        "backend": backend,
        "state": state,
        "outcome": state,
        "workspace": "/tmp",
        "created_at": runners._now(),
        "started_at": runners._now(),
        "ended_at": None,
        "pid": None,
        "returncode": None,
        "error": "",
    })
    runners._atomic_json(request_path, {"backend": backend, "contract": _contract(Path("/tmp"), backend=backend), "timeout": 30})
    return meta_path


def test_cancel_marker_wins_over_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    task_id = "race-completed-001"
    meta_path, _, _ = runners._job_paths(task_id, tmp_path)
    root = meta_path.parent
    root.mkdir(parents=True, exist_ok=True)
    _make_job(root, task_id)
    cancel_path = root / f"{task_id}.cancel.json"
    runners._atomic_json(cancel_path, {"task_id": task_id})
    monkeypatch.setattr(runners, "get_backend", lambda name: _fake_backend(monkeypatch))
    monkeypatch.setattr(runners, "_worker_pi", lambda exe, contract, timeout, log_path, hermes_root=None: (0, "done"))
    rc = runners._worker(task_id, root)
    meta = json.loads(meta_path.read_text())
    assert rc == 0, meta.get("error")
    assert meta["state"] == "cancelled"
    assert meta["outcome"] == "cancelled"
    assert not (root / f"{task_id}.cancel.json").exists()


def test_cancel_marker_wins_over_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    task_id = "race-failed-001"
    meta_path, _, _ = runners._job_paths(task_id, tmp_path)
    root = meta_path.parent
    root.mkdir(parents=True, exist_ok=True)
    _make_job(root, task_id)
    cancel_path = root / f"{task_id}.cancel.json"
    runners._atomic_json(cancel_path, {"task_id": task_id})
    monkeypatch.setattr(runners, "get_backend", lambda name: _fake_backend(monkeypatch))
    monkeypatch.setattr(runners, "_worker_omx", lambda exe, contract, timeout, log_path: (3, ""))
    runners._worker(task_id, root)
    meta = json.loads(meta_path.read_text())
    assert meta["state"] == "cancelled"


def test_cancel_marker_wins_on_exception_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    task_id = "race-exc-001"
    meta_path, _, _ = runners._job_paths(task_id, tmp_path)
    root = meta_path.parent
    root.mkdir(parents=True, exist_ok=True)
    _make_job(root, task_id)
    cancel_path = root / f"{task_id}.cancel.json"
    runners._atomic_json(cancel_path, {"task_id": task_id})

    def _raise(name):
        raise LookupError("backend vanished")

    monkeypatch.setattr(runners, "get_backend", _raise)
    assert runners._worker(task_id, root) == 1
    meta = json.loads(meta_path.read_text())
    assert meta["state"] == "cancelled"
    assert not (root / f"{task_id}.cancel.json").exists()


def test_worker_without_cancel_marker_reports_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    task_id = "race-ok-001"
    meta_path, _, _ = runners._job_paths(task_id, tmp_path)
    root = meta_path.parent
    root.mkdir(parents=True, exist_ok=True)
    _make_job(root, task_id)
    monkeypatch.setattr(runners, "get_backend", lambda name: _fake_backend(monkeypatch))
    monkeypatch.setattr(runners, "_worker_pi", lambda exe, contract, timeout, log_path, hermes_root=None: (0, "done"))
    rc = runners._worker(task_id, root)
    meta = json.loads(meta_path.read_text())
    assert rc == 0, meta.get("error")
    assert meta["state"] == "completed"
    assert not (root / f"{task_id}.cancel.json").exists()


def test_cancel_arriving_during_terminal_write_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    task_id = "race-terminal-write-001"
    meta_path, _, _ = runners._job_paths(task_id, tmp_path)
    root = meta_path.parent
    root.mkdir(parents=True, exist_ok=True)
    _make_job(root, task_id)
    cancel_path = root / f"{task_id}.cancel.json"
    monkeypatch.setattr(runners, "get_backend", lambda name: _fake_backend(monkeypatch))
    monkeypatch.setattr(runners, "_worker_pi", lambda exe, contract, timeout, log_path, hermes_root=None: (0, "done"))
    real_atomic = runners._atomic_json
    injected = {"done": False}

    def _atomic_with_cancel(path, value):
        real_atomic(path, value)
        if path == meta_path and value.get("state") == "completed" and not injected["done"]:
            injected["done"] = True
            real_atomic(cancel_path, {"task_id": task_id, "requested_at": runners._now()})

    monkeypatch.setattr(runners, "_atomic_json", _atomic_with_cancel)
    rc = runners._worker(task_id, root)
    meta = json.loads(meta_path.read_text())
    assert rc == 0
    assert injected["done"] is True
    assert meta["state"] == "cancelled"
    assert meta["outcome"] == "cancelled"
    assert not cancel_path.exists()


class _ExternalBackend:
    name = "external_probe"

    def availability(self, *, hermes_root=None):
        return {"available": True}

    def dispatch(self, *a, **k):
        return {"success": True}

    def observed_runs(self, task_id, *, hermes_root=None):
        return []

    def cancel(self, task_id, *, hermes_root=None):
        return {"success": True}


class _ShadowFleetBackend(_ExternalBackend):
    name = "fleet"


def _fake_entry_points(monkeypatch, candidates):
    class _EP:
        def __init__(self, name, loader):
            self.name = name
            self._loader = loader

        def load(self):
            return self._loader

    class _EPS(dict):
        def select(self, *, group):
            return [_EP(name, loader) for name, loader in candidates.get(group, [])]

    monkeypatch.setattr(runners.importlib.metadata, "entry_points", lambda: _EPS())


def test_plugin_class_entry_point_instantiates(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(runners.RUNNER_PLUGIN_ALLOWLIST_ENV, "external_probe")
    _fake_entry_points(monkeypatch, {"hermes_gpt.runners": [("external_probe", _ExternalBackend)]})
    loaded = runners.load_entrypoint_backends()
    assert "external_probe" in loaded
    try:
        assert runners.get_backend("external_probe").availability() == {"available": True}
    finally:
        with runners._REGISTRY_LOCK:
            runners._BACKENDS.pop("external_probe", None)


def test_plugin_cannot_shadow_builtin_backend_name(monkeypatch: pytest.MonkeyPatch):
    _fake_entry_points(monkeypatch, {"hermes_gpt.runners": [("fleet", _ShadowFleetBackend)]})
    loaded = runners.load_entrypoint_backends()
    assert loaded == []
    with pytest.raises(LookupError):
        runners.get_backend("__never__")  # registry sanity helper
    # The built-in fleet backend must still be the registered one.
    assert runners.get_backend("fleet").__class__ is runners.FleetBackend



def test_plugin_entry_point_requires_explicit_allowlist(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(runners.RUNNER_PLUGIN_ALLOWLIST_ENV, raising=False)
    _fake_entry_points(monkeypatch, {"hermes_gpt.runners": [("external_probe", _ExternalBackend)]})
    loaded = runners.load_entrypoint_backends()
    assert loaded == []
    with pytest.raises(LookupError):
        runners.get_backend("external_probe")


def test_runner_backend_allowlist_blocks_unexpected_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(runners.RUNNER_BACKEND_ALLOWLIST_ENV, "fleet")
    raw = _contract(tmp_path, backend="pi_rpc")
    payload = runners.dispatch_contract(raw, confirm=False, dry_run=True, timeout=30, hermes_root=tmp_path / "hermes")
    assert payload["success"] is False
    assert payload["code"] == "RUNNER_BACKEND_NOT_ALLOWED"


def test_runner_provider_model_allowlists_are_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _enable_pi_confinement(monkeypatch)
    pi_dir = tmp_path / "pi-agent"
    pi_dir.mkdir()
    (pi_dir / "settings.json").write_text(json.dumps({"defaultProvider": "expensive-provider", "defaultModel": "expensive-model"}), encoding="utf-8")
    (pi_dir / "models.json").write_text(json.dumps({"providers": {}}), encoding="utf-8")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_dir))
    monkeypatch.setenv(runners.RUNNER_PROVIDER_ALLOWLIST_ENV, "safe-provider")
    raw = _contract(tmp_path, backend="pi_rpc")
    raw["authorization"] = {"class": "read_only", "approved": True}
    backend = runners.PiRpcBackend()
    with pytest.raises(PermissionError, match="not allowed"):
        backend.build_plan(raw)


def test_windows_process_tree_cleanup_uses_taskkill(monkeypatch: pytest.MonkeyPatch):
    calls = []

    class _Proc:
        pid = 4242
        waits = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.waits += 1
            return 0

        def terminate(self):
            calls.append(["terminate"])

        def kill(self):
            calls.append(["kill"])

    def _run(argv, **kwargs):
        calls.append(argv)
        return runners.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(runners.os, "name", "nt")
    monkeypatch.setattr(runners.subprocess, "run", _run)
    runners._terminate_process_tree(_Proc(), timeout=1)
    assert ["taskkill", "/PID", "4242", "/T", "/F"] in calls
    assert ["terminate"] not in calls
    assert ["kill"] not in calls


@pytest.mark.parametrize("failure", ["missing", "nonzero"])
def test_windows_detached_pid_cleanup_falls_back_when_taskkill_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
):
    direct_kills = []

    def _run(argv, **kwargs):
        if failure == "missing":
            raise FileNotFoundError("taskkill unavailable")
        return runners.subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(runners.os, "name", "nt")
    monkeypatch.setattr(runners.subprocess, "run", _run)
    monkeypatch.setattr(runners.os, "kill", lambda pid, sig: direct_kills.append((pid, sig)))

    runners._terminate_process_tree(5252, timeout=1)

    assert direct_kills == [(5252, runners.signal.SIGTERM)]


def test_posix_process_tree_cleanup_uses_process_group(monkeypatch: pytest.MonkeyPatch):
    calls = []

    class _Proc:
        pid = 4343
        waits = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.waits += 1
            return 0

    monkeypatch.setattr(runners.os, "name", "posix")
    monkeypatch.setattr(runners.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    runners._terminate_process_tree(_Proc(), timeout=1)
    assert calls == [(4343, runners.signal.SIGTERM)]


def test_stale_request_envelope_cleanup_removes_old_prompt(tmp_path: Path):
    task_id = "stale-request-001"
    _, request_path, _ = runners._job_paths(task_id, tmp_path)
    runners._atomic_json(request_path, {"contract": {"objective": "stale raw prompt"}})
    old = time.time() - 7200
    request_path.touch()
    import os as _os
    _os.utime(request_path, (old, old))
    assert runners._cleanup_stale_request_envelopes(hermes_root=tmp_path, ttl_seconds=3600) == 1
    assert not request_path.exists()
