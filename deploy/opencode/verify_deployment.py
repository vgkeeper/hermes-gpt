from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SERVER_NAME = "hermes-opencode"
PROXY_NAME = "hermes-opencode-auth-proxy"
NETWORK_NAME = "hermes-opencode-int"
WORKSPACE_TARGET = "/workspaces"
SERVER_COMMAND = [
    "/opt/opencode/node_modules/.bin/opencode",
    "serve",
    "--hostname",
    "127.0.0.1",
    "--port",
    "4096",
    "--pure",
    "--log-level",
    "ERROR",
]
EXPECTED_MOUNTS = {
    "/opt/opencode",
    "/home/node/.local",
    "/opencode.json",
    "/workspaces",
    "/home/node/.config/opencode/agents",
}
EXPECTED_PROXY_MOUNTS = {
    "/etc/nginx/nginx.conf",
    "/run/secrets/opencode.htpasswd",
}
EXPECTED_SERVER_TMPFS = {
    "/tmp": {"size": 64 * 1024**2, "uid": 1000, "gid": 1000},
    "/home/node/.config/opencode": {"size": 16 * 1024**2, "uid": 1000, "gid": 1000},
}
EXPECTED_PROXY_TMPFS = {"/tmp": {"size": 16 * 1024**2, "uid": 101, "gid": 101, "mode": "0700"}}

_INSPECT_TEMPLATE = r'''{"id":{{json .Id}},"running":{{json .State.Running}},"config":{"image":{{json .Config.Image}},"user":{{json .Config.User}},"cmd":{{json .Config.Cmd}},"env_names":[{{range $i, $env := .Config.Env}}{{if $i}},{{end}}{{json (index (split $env "=") 0)}}{{end}}],"env_presence":[{{range $i, $env := .Config.Env}}{{if $i}},{{end}}{{ $parts := split $env "=" }}{"name":{{json (index $parts 0)}},"empty":{{if eq (len $parts) 2}}{{if eq (index $parts 1) ""}}true{{else}}false{{end}}{{else}}false{{end}}}{{end}}]},"host":{"readonly_rootfs":{{json .HostConfig.ReadonlyRootfs}},"cap_drop":{{json .HostConfig.CapDrop}},"cap_add":{{json .HostConfig.CapAdd}},"security_opt":{{json .HostConfig.SecurityOpt}},"tmpfs":{{json .HostConfig.Tmpfs}},"memory":{{json .HostConfig.Memory}},"nano_cpus":{{json .HostConfig.NanoCpus}},"pids_limit":{{json .HostConfig.PidsLimit}},"restart":{{json .HostConfig.RestartPolicy}},"network_mode":{{json .HostConfig.NetworkMode}},"port_bindings":{{json .HostConfig.PortBindings}}},"networks":{{json .NetworkSettings.Networks}},"ports":{{json .NetworkSettings.Ports}},"mounts":{{json .Mounts}}}'''
_SECRET_ENV_RE = re.compile(r"(?:API[_-]?KEY|TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL)", re.IGNORECASE)
_REMOTE_AUTH_ENV_RE = re.compile(r"OPENCODE.*(?:PASSWORD|PASSWD|TOKEN|SECRET|CREDENTIAL|AUTH)|(?:PASSWORD|PASSWD|TOKEN|SECRET|CREDENTIAL|AUTH).*OPENCODE", re.IGNORECASE)


def _mount(container: dict[str, Any], destination: str) -> dict[str, Any] | None:
    mounts = container.get("mounts")
    if not isinstance(mounts, list):
        return None
    found = [item for item in mounts if isinstance(item, dict) and item.get("Destination") == destination]
    return found[0] if len(found) == 1 else None


def _matches_source(mount: dict[str, Any] | None, expected: Path) -> bool:
    if not mount or not isinstance(mount.get("Source"), str):
        return False
    try:
        return Path(mount["Source"]).resolve(strict=True) == expected
    except (OSError, RuntimeError):
        return False


def _has_no_published_ports(container: dict[str, Any]) -> bool:
    host_bindings = container.get("host", {}).get("port_bindings") or {}
    runtime_ports = container.get("ports") or {}
    return not any(bool(bindings) for bindings in host_bindings.values()) and not any(
        bool(bindings) for bindings in runtime_ports.values()
    )


def _parse_tmpfs(value: Any) -> dict[str, dict[str, str]] | None:
    if not isinstance(value, dict):
        return None
    parsed: dict[str, dict[str, str]] = {}
    for destination, options in value.items():
        if not isinstance(destination, str) or not isinstance(options, str):
            return None
        fields: dict[str, str] = {}
        for item in options.split(","):
            if "=" in item:
                key, field_value = item.split("=", 1)
                fields[key] = field_value
            else:
                fields[item] = ""
        parsed[destination] = fields
    return parsed


def _size_bytes(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.fullmatch(r"([0-9]+)([kmg]?)", value.lower())
    if not match:
        return None
    multiplier = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[match.group(2)]
    return int(match.group(1)) * multiplier


def _tmpfs_is_safe(host: dict[str, Any], expected_mounts: dict[str, dict[str, Any]]) -> bool:
    actual = _parse_tmpfs(host.get("tmpfs"))
    if actual is None or set(actual) != set(expected_mounts):
        return False
    for destination, expected in expected_mounts.items():
        fields = actual[destination]
        required = {"rw": "", "noexec": "", "nosuid": "", "nodev": ""}
        if any(fields.get(key) != value for key, value in required.items()):
            return False
        for key, value in expected.items():
            if key == "size":
                if _size_bytes(fields.get(key)) != int(value):
                    return False
            elif fields.get(key) != str(value):
                return False
        if set(fields) != set(required) | set(expected):
            return False
    return True


def _has_hardening(host: dict[str, Any], *, memory: int, nano_cpus: int, pids: int) -> bool:
    security_options = host.get("security_opt") or []
    if not isinstance(security_options, list) or not all(isinstance(item, str) for item in security_options):
        return False
    unsafe_options = ("seccomp=unconfined", "apparmor=unconfined")
    return (
        host.get("readonly_rootfs") is True
        and "ALL" in (host.get("cap_drop") or [])
        and not host.get("cap_add")
        and not any(option.lower() in unsafe_options for option in security_options)
        and any(item in {"no-new-privileges:true", "no-new-privileges"} for item in security_options)
        and host.get("memory") == memory
        and host.get("nano_cpus") == nano_cpus
        and host.get("pids_limit") == pids
    )


def _parse_proc_status(output: str) -> bool:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if separator and key in {"Seccomp", "NoNewPrivs"}:
            values[key] = value.strip()
    return values == {"Seccomp": "2", "NoNewPrivs": "1"}


def _safe_env_names(container: dict[str, Any]) -> list[str] | None:
    names = (container.get("config") or {}).get("env_names")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        return None
    return names


def _safe_env_empty_flags(container: dict[str, Any]) -> dict[str, bool] | None:
    names = _safe_env_names(container)
    entries = (container.get("config") or {}).get("env_presence")
    if names is None or not isinstance(entries, list):
        return None
    flags: dict[str, bool] = {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or not isinstance(entry.get("empty"), bool)
            or entry["name"] in flags
        ):
            return None
        flags[entry["name"]] = entry["empty"]
    return flags if set(flags) == set(names) else None


def _env_values_absent_or_empty(container: dict[str, Any], variable_names: tuple[str, ...]) -> bool:
    flags = _safe_env_empty_flags(container)
    return flags is not None and all(flags.get(name, True) for name in variable_names)


def _env_is_present_and_nonempty(container: dict[str, Any], variable_name: str) -> bool:
    flags = _safe_env_empty_flags(container)
    return flags is not None and variable_name in flags and not flags[variable_name]


def _webui_env_errors(container: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    names = _safe_env_names(container)
    if names is None or any(_REMOTE_AUTH_ENV_RE.search(name) for name in names):
        errors.append("WebUI environment names are uninspectable or contain OpenCode remote auth")
    if not _env_is_present_and_nonempty(container, "HERMES_WEBUI_PASSWORD"):
        errors.append("WebUI HERMES_WEBUI_PASSWORD must be present and nonempty")
    if not _env_values_absent_or_empty(container, ("SERVICE_PASSWORD_HERMESWEBUI",)):
        errors.append("WebUI SERVICE_PASSWORD_HERMESWEBUI must be absent or empty")
    return errors


def _gateway_workspace_matches(mount: dict[str, Any] | None, workspace_source: Path) -> bool:
    if not mount or not isinstance(mount.get("Source"), str):
        return False
    try:
        return Path(mount["Source"]).resolve(strict=True) / "opencode-workspaces" == workspace_source
    except (OSError, RuntimeError):
        return False


def probe_proc_security(container_name: str) -> bool:
    command = "awk '/^(Seccomp|NoNewPrivs):/ { print $1, $2 }' /proc/1/status"
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "sh", "-c", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and _parse_proc_status(result.stdout[:4096])


def validate_deployment(
    server: dict[str, Any],
    proxy: dict[str, Any],
    gateway: dict[str, Any],
    agent: dict[str, Any],
    webui: dict[str, Any] | None = None,
    *,
    network_internal: bool,
    workspace_source: Path,
    config_file: Path,
    agent_config_dir: Path,
    server_proc_secure: bool,
    proxy_proc_secure: bool,
) -> None:
    errors: list[str] = []
    server_host = server.get("host") or {}
    server_config = server.get("config") or {}
    if server_config.get("image") != "node:22-bookworm":
        errors.append("OpenCode server image is not node:22-bookworm")
    if server_config.get("user") != "1000:1000" or server_config.get("cmd") != SERVER_COMMAND:
        errors.append("OpenCode server command or UID:GID does not match the approved runtime")
    if server.get("running") is not True:
        errors.append("OpenCode server container is not running")
    if not _has_hardening(server_host, memory=2 * 1024**3, nano_cpus=1_000_000_000, pids=256):
        errors.append("OpenCode server rootfs, capabilities, privilege, or resource limits are unsafe")
    if not server_proc_secure:
        errors.append("OpenCode server /proc/1/status does not attest Seccomp: 2 and NoNewPrivs: 1")
    if not _tmpfs_is_safe(server_host, EXPECTED_SERVER_TMPFS):
        errors.append("OpenCode server tmpfs mounts do not match the approved runtime")
    server_env_names = _safe_env_names(server)
    if server_env_names is None or any(
        _SECRET_ENV_RE.search(name) or _REMOTE_AUTH_ENV_RE.search(name) for name in server_env_names
    ):
        errors.append("OpenCode server environment contains a secret-bearing variable name")
    if (server_host.get("restart") or {}).get("Name") != "unless-stopped":
        errors.append("OpenCode server restart policy is not unless-stopped")
    if not _has_no_published_ports(server):
        errors.append("OpenCode server publishes a host port")
    if not network_internal or set((server.get("networks") or {}).keys()) != {NETWORK_NAME}:
        errors.append("OpenCode server is not isolated on the internal hermes-opencode-int network")
    if NETWORK_NAME not in (gateway.get("networks") or {}):
        errors.append("Hermes Gateway is not connected to hermes-opencode-int")

    proxy_host = proxy.get("host") or {}
    proxy_config = proxy.get("config") or {}
    if proxy.get("running") is not True:
        errors.append("OpenCode auth proxy container is not running")
    if proxy_config.get("user") != "101:101":
        errors.append("OpenCode auth proxy does not run as UID:GID 101:101")
    if not _has_hardening(proxy_host, memory=128 * 1024**2, nano_cpus=250_000_000, pids=32):
        errors.append("OpenCode auth proxy rootfs, capabilities, privilege, or resource limits are unsafe")
    if not proxy_proc_secure:
        errors.append("OpenCode auth proxy /proc/1/status does not attest Seccomp: 2 and NoNewPrivs: 1")
    if not _tmpfs_is_safe(proxy_host, EXPECTED_PROXY_TMPFS):
        errors.append("OpenCode auth proxy tmpfs does not match the approved runtime")
    proxy_env_names = _safe_env_names(proxy)
    if proxy_env_names is None or any(
        _SECRET_ENV_RE.search(name) or _REMOTE_AUTH_ENV_RE.search(name) for name in proxy_env_names
    ):
        errors.append("OpenCode auth proxy environment contains a secret-bearing variable name")
    proxy_mounts = proxy.get("mounts") or []
    proxy_destinations = {item.get("Destination") for item in proxy_mounts if isinstance(item, dict)}
    if proxy_destinations != EXPECTED_PROXY_MOUNTS:
        errors.append("OpenCode auth proxy mounts are not limited to its config and verifier")
    for destination in ("/etc/nginx/nginx.conf", "/run/secrets/opencode.htpasswd"):
        mount = _mount(proxy, destination)
        if not mount or mount.get("Type") != "bind" or mount.get("RW") is not False:
            errors.append("OpenCode auth proxy config and verifier mounts must be read-only")
            break
    if proxy_host.get("network_mode") not in {
        f"container:{server.get('id')}",
        f"container:{SERVER_NAME}",
    }:
        errors.append("OpenCode auth proxy does not share the server network namespace")
    if not _has_no_published_ports(proxy):
        errors.append("OpenCode auth proxy publishes a host port")

    server_mounts = server.get("mounts") or []
    destinations = {item.get("Destination") for item in server_mounts if isinstance(item, dict)}
    if destinations != EXPECTED_MOUNTS:
        errors.append("OpenCode server mounts are not limited to the approved application, state, workspace, and config mounts")

    app_mount = _mount(server, "/opt/opencode")
    if not app_mount or app_mount.get("Type") != "volume" or app_mount.get("Name") != "hermes-opencode-app" or app_mount.get("RW") is not False:
        errors.append("OpenCode application volume is not hermes-opencode-app mounted read-only")
    state_mount = _mount(server, "/home/node/.local")
    if not state_mount or state_mount.get("Type") != "volume" or state_mount.get("Name") != "hermes-opencode-state" or state_mount.get("RW") is not True:
        errors.append("OpenCode state volume is not hermes-opencode-state")

    for destination, expected in (
        ("/opencode.json", config_file),
        ("/home/node/.config/opencode/agents", agent_config_dir),
    ):
        mount = _mount(server, destination)
        if not mount or mount.get("Type") != "bind" or mount.get("RW") is not False or not _matches_source(mount, expected):
            errors.append(f"OpenCode config mount {destination} is not the expected read-only source")

    remote_workspace = _mount(server, WORKSPACE_TARGET)
    gateway_data = _mount(gateway, "/opt/data")
    if (
        not remote_workspace
        or remote_workspace.get("Type") != "bind"
        or remote_workspace.get("RW") is not False
        or not _matches_source(remote_workspace, workspace_source)
    ):
        errors.append("OpenCode workspace mount is not the expected read-only dedicated source")
    if (
        not gateway_data
        or gateway_data.get("Type") != "bind"
        or gateway_data.get("RW") is not True
        or not _gateway_workspace_matches(gateway_data, workspace_source)
    ):
        errors.append("Gateway /opt/data does not contain the expected writable opencode-workspaces source")

    gateway_names = _safe_env_names(gateway)
    if gateway_names is None or "HERMES_GPT_OPENCODE_SERVER_PASSWORD" not in gateway_names:
        errors.append("Hermes Gateway does not expose the remote Basic password variable name")
    if not _env_values_absent_or_empty(
        gateway, ("HERMES_WEBUI_PASSWORD", "SERVICE_PASSWORD_HERMESWEBUI")
    ):
        errors.append("Hermes Gateway WebUI password variables must be absent or empty")
    agent_names = _safe_env_names(agent)
    if agent_names is None or any(_REMOTE_AUTH_ENV_RE.search(name) for name in agent_names):
        errors.append("Hermes Agent environment names are uninspectable or contain OpenCode remote auth")
    if not _env_values_absent_or_empty(
        agent, ("HERMES_WEBUI_PASSWORD", "SERVICE_PASSWORD_HERMESWEBUI")
    ):
        errors.append("Hermes Agent WebUI password variables must be absent or empty")
    if webui is not None:
        errors.extend(_webui_env_errors(webui))

    if errors:
        raise ValueError("OpenCode deployment preflight failed: " + "; ".join(errors))


def _docker_json(*args: str) -> Any:
    try:
        result = subprocess.run(
            ["docker", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Docker preflight command failed") from exc
    if result.returncode:
        raise RuntimeError("Docker preflight could not inspect the configured container/network")
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Docker preflight returned invalid inspection data") from exc


def inspect_container(name: str) -> dict[str, Any]:
    value = _docker_json("inspect", "--type=container", "--format", _INSPECT_TEMPLATE, name)
    if not isinstance(value, dict):
        raise RuntimeError("Docker container inspection was invalid")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the live OpenCode remote-attach container boundary.")
    parser.add_argument("--gateway-container", required=True, help="Docker container name for the Hermes Gateway")
    parser.add_argument("--agent-container", required=True, help="Docker container name for the Hermes Agent")
    parser.add_argument("--webui-container", help="Optional Docker container name for the WebUI")
    args = parser.parse_args()
    try:
        source_raw = os.environ.get("OPENCODE_WORKSPACE_SOURCE", "")
        config_raw = os.environ.get("OPENCODE_CONFIG_FILE", "")
        agent_raw = os.environ.get("OPENCODE_AGENT_CONFIG_DIR", "")
        if not source_raw or not config_raw or not agent_raw:
            raise RuntimeError("Set OPENCODE_WORKSPACE_SOURCE, OPENCODE_CONFIG_FILE, and OPENCODE_AGENT_CONFIG_DIR")
        workspace_source = Path(source_raw).expanduser().resolve(strict=True)
        config_file = Path(config_raw).expanduser().resolve(strict=True)
        agent_config_dir = Path(agent_raw).expanduser().resolve(strict=True)
        if (
            not workspace_source.is_dir()
            or not config_file.is_file()
            or not agent_config_dir.is_dir()
            or not (agent_config_dir / "hermes-readonly.md").is_file()
        ):
            raise RuntimeError("OpenCode workspace/config/agent source paths have the wrong type")
        server = inspect_container(SERVER_NAME)
        proxy = inspect_container(PROXY_NAME)
        gateway = inspect_container(args.gateway_container)
        agent = inspect_container(args.agent_container)
        webui = inspect_container(args.webui_container) if args.webui_container else None
        server_proc_secure = probe_proc_security(SERVER_NAME)
        proxy_proc_secure = probe_proc_security(PROXY_NAME)
        internal = _docker_json("network", "inspect", "--format", "{{json .Internal}}", NETWORK_NAME)
        validate_deployment(
            server,
            proxy,
            gateway,
            agent,
            webui,
            network_internal=internal is True,
            workspace_source=workspace_source,
            config_file=config_file,
            agent_config_dir=agent_config_dir,
            server_proc_secure=server_proc_secure,
            proxy_proc_secure=proxy_proc_secure,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("FAIL: deployment source paths are inaccessible", file=sys.stderr)
        return 1
    webui_status = "verified" if webui is not None else "not-checked"
    print(f"PASS: server/proxy hardening and mounts verified; Gateway/Agent WebUI password isolation checked; WebUI={webui_status}; Seccomp=2 NoNewPrivs=1; run authenticated hermes_runner_list next.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
