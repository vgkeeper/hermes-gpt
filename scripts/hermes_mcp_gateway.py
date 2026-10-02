"""Supervise the dedicated MCP Events gateway's loopback server and tunnel client."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HERMES_HOME = "/home/hermes/.hermes"
EXPECTED_SERVER_HOME = "/opt/data"
INFISICAL_HOME = "/home/hermes/.hermes/home"
SERVER_URL = "http://127.0.0.1:17678/"
SERVER_COMMAND = (
    "--http",
    "--host",
    "127.0.0.1",
    "--port",
    "17678",
)
TUNNEL_CLIENT = "/usr/local/bin/tunnel-client-runtime"
INFISICAL = "/home/hermes/.hermes/home/.local/bin/infisical"
TUNNEL_ID_ENV = "HERMES_MCP_GATEWAY_TUNNEL_ID"
TUNNEL_ENABLED_ENV = "HERMES_MCP_GATEWAY_TUNNEL_ENABLED"

_shutdown_requested = False


def _flag_enabled() -> bool:
    value = os.getenv(TUNNEL_ENABLED_ENV, "0").strip()
    if value not in {"0", "1"}:
        raise RuntimeError("invalid tunnel-enabled setting")
    return value == "1"


def _validate_runtime_paths() -> None:
    if os.getenv("HERMES_HOME") != EXPECTED_HERMES_HOME:
        raise RuntimeError("HERMES_HOME does not point at the shared Hermes state")
    if os.getenv("HOME") != EXPECTED_SERVER_HOME:
        raise RuntimeError("HOME does not match the primary Hermes runtime")
    if os.getenv("HERMES_PROFILE") != "default":
        raise RuntimeError("gateway must use the default Hermes profile")


def server_command() -> list[str]:
    return [sys.executable, str(PROJECT_ROOT / "server.py"), *SERVER_COMMAND]


def server_environment() -> dict[str, str]:
    env = os.environ.copy()
    # The MCP server and Hermes job subprocesses do not need the tunnel key or
    # the short-lived Infisical token. Only the tunnel-client child receives the
    # API key injected by the separate Infisical run below.
    for name in (
        "CONTROL_PLANE_API_KEY",
        "INFISICAL_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return env


def tunnel_command() -> list[str]:
    tunnel_id = os.getenv(TUNNEL_ID_ENV, "")
    if not re.fullmatch(r"tunnel_[0-9a-f]{32}", tunnel_id):
        raise RuntimeError("configured tunnel ID has an invalid format")
    return [
        INFISICAL,
        "run",
        "--env=dev",
        "--path=/mcp-events-staging",
        "--",
        "env",
        "-u",
        "INFISICAL_TOKEN",
        TUNNEL_CLIENT,
        "run",
        "--control-plane.base-url",
        "https://api.openai.com",
        "--mcp.server-url",
        "url=http://127.0.0.1:17678/mcp,channel=main",
        "--control-plane.tunnel-id",
        tunnel_id,
        "--health.listen-addr",
        "127.0.0.1:17679",
        "--log-format",
        "json",
    ]


def _server_ready(proc: subprocess.Popen[Any], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not _shutdown_requested:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(SERVER_URL, timeout=2) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.25)
    return False


def _signal_handler(_signum: int, _frame: Any) -> None:
    global _shutdown_requested
    _shutdown_requested = True


def _stop_processes(processes: list[tuple[str, subprocess.Popen[Any]]]) -> None:
    for _name, proc in reversed(processes):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for _name, proc in reversed(processes):
        if proc.poll() is None:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)


def main() -> int:
    try:
        _validate_runtime_paths()
        use_tunnel = _flag_enabled()
        if use_tunnel:
            for path in (INFISICAL, TUNNEL_CLIENT):
                if not Path(path).is_file() or not os.access(path, os.X_OK):
                    raise RuntimeError("required tunnel runtime executable is unavailable")
            # Validate the non-secret identifier before starting the MCP server.
            tunnel_command()
    except RuntimeError as exc:
        print(f"hermes-mcp-gateway configuration error: {exc}", file=sys.stderr, flush=True)
        return 2

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    processes: list[tuple[str, subprocess.Popen[Any]]] = []
    try:
        server = subprocess.Popen(
            server_command(),
            env=server_environment(),
            start_new_session=True,
        )
        processes.append(("mcp-server", server))
        if not _server_ready(server):
            print("hermes-mcp-gateway: MCP server did not become ready", file=sys.stderr, flush=True)
            return 1
        print("hermes-mcp-gateway: MCP server ready", flush=True)

        if use_tunnel:
            tunnel_env = {
                key: os.environ[key]
                for key in ("LANG", "LC_ALL", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR")
                if key in os.environ
            }
            # The Infisical facade and Universal Auth config live under the
            # nested home in the shared Hermes volume; the MCP server itself
            # keeps the primary runtime's HOME=/opt/data.
            tunnel_env["HOME"] = INFISICAL_HOME
            tunnel_env["PATH"] = f"{INFISICAL_HOME}/.local/bin:/usr/bin:/bin"
            tunnel = subprocess.Popen(
                tunnel_command(),
                env=tunnel_env,
                start_new_session=True,
            )
            processes.append(("tunnel-client", tunnel))
            print("hermes-mcp-gateway: tunnel-client started", flush=True)
        else:
            print("hermes-mcp-gateway: tunnel disabled for local validation", flush=True)

        while not _shutdown_requested:
            for name, proc in processes:
                code = proc.poll()
                if code is not None:
                    print(
                        f"hermes-mcp-gateway: {name} exited unexpectedly ({code})",
                        file=sys.stderr,
                        flush=True,
                    )
                    return code if code != 0 else 1
            time.sleep(0.25)
        return 0
    except OSError as exc:
        print(f"hermes-mcp-gateway: child startup failed ({type(exc).__name__})", file=sys.stderr, flush=True)
        return 1
    finally:
        _stop_processes(processes)


if __name__ == "__main__":
    raise SystemExit(main())
