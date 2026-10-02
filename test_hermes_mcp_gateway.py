from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gateway = _load("hermes_mcp_gateway", ROOT / "scripts/hermes_mcp_gateway.py")
health = _load("hermes_mcp_gateway_healthcheck", ROOT / "scripts/hermes_mcp_gateway_healthcheck.py")


def test_server_command_is_loopback_only_and_uses_checkout_source():
    command = gateway.server_command()
    assert command[1] == str(ROOT / "server.py")
    assert command[2:] == ["--http", "--host", "127.0.0.1", "--port", "17678"]


def test_runtime_guard_requires_shared_home_and_hermes_python(monkeypatch):
    monkeypatch.setattr(gateway.sys, "executable", gateway.EXPECTED_RUNTIME_PYTHON)
    monkeypatch.setenv("HERMES_HOME", gateway.EXPECTED_HERMES_HOME)
    monkeypatch.setenv("HOME", gateway.EXPECTED_SERVER_HOME)
    monkeypatch.setenv("HERMES_PROFILE", "default")
    gateway._validate_runtime_paths()

    monkeypatch.setattr(gateway.sys, "executable", str(ROOT / ".venv/bin/python"))
    with pytest.raises(RuntimeError, match="Agent runtime Python"):
        gateway._validate_runtime_paths()


def test_server_environment_drops_tunnel_and_provider_secrets(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/home/hermes/.hermes")
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for name in (
        "CONTROL_PLANE_API_KEY",
        "INFISICAL_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.setenv(name, "test-only-redacted-value")

    env = gateway.server_environment()

    assert env["HERMES_HOME"] == "/home/hermes/.hermes"
    assert env["HERMES_PROFILE"] == "default"
    assert env["PYTHONPATH"] == str(ROOT)
    assert all(
        name not in env
        for name in (
            "CONTROL_PLANE_API_KEY",
            "INFISICAL_TOKEN",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "OPENROUTER_API_KEY",
        )
    )


def test_tunnel_command_uses_infisical_and_fixed_loopback_target(monkeypatch):
    tunnel_id = "tunnel_0123456789abcdef0123456789abcdef"
    monkeypatch.setenv(gateway.TUNNEL_ID_ENV, tunnel_id)

    command = gateway.tunnel_command()

    assert command[:5] == [
        gateway.INFISICAL,
        "run",
        "--env=dev",
        "--path=/mcp-events-staging",
        "--",
    ]
    assert command[command.index("--control-plane.base-url") + 1] == "https://api.openai.com"
    assert command[command.index("--mcp.server-url") + 1] == "url=http://127.0.0.1:17678/mcp,channel=main"
    assert command[command.index("--control-plane.tunnel-id") + 1] == tunnel_id
    assert command[command.index("--log.format") + 1] == "json"
    assert "--api-key" not in command
    assert "CONTROL_PLANE_API_KEY" not in command
    assert "INFISICAL_TOKEN" in command


def test_tunnel_command_rejects_malformed_id(monkeypatch):
    monkeypatch.setenv(gateway.TUNNEL_ID_ENV, "tunnel-not-valid")
    with pytest.raises(RuntimeError, match="invalid format"):
        gateway.tunnel_command()


class _Response:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_healthcheck_checks_tunnel_only_when_enabled(monkeypatch):
    monkeypatch.setenv(health.TUNNEL_ENABLED_ENV, "0")
    with patch.object(health.urllib.request, "urlopen", side_effect=[_Response(200)]) as opened:
        assert health.main() == 0
        opened.assert_called_once_with(health.SERVER_HEALTH_URL, timeout=2)

    monkeypatch.setenv(health.TUNNEL_ENABLED_ENV, "1")
    with patch.object(
        health.urllib.request,
        "urlopen",
        side_effect=[_Response(200), _Response(200)],
    ) as opened:
        assert health.main() == 0
        assert [call.args[0] for call in opened.call_args_list] == [
            health.SERVER_HEALTH_URL,
            health.TUNNEL_READY_URL,
        ]

    with patch.object(
        health.urllib.request,
        "urlopen",
        side_effect=[_Response(200), _Response(503)],
    ):
        assert health.main() == 1
