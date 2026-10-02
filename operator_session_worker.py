"""Durable session-control worker that owns a shared-filesystem lease."""

from __future__ import annotations

import argparse

try:
    import fcntl
except ImportError:  # pragma: no cover - worker requires POSIX locks in production
    fcntl = None
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HEARTBEAT_SECONDS = 2.0
LEASE_SECONDS = 12.0
_shutdown = threading.Event()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def _proc_start_token(pid: int) -> str:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        right = stat.rfind(")")
        tail = stat[right + 2 :].split()
        return tail[19] if right >= 0 and len(tail) > 19 else ""
    except OSError:
        return ""


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_owner_state(config: dict[str, Any], state: str, return_code: int | None = None) -> None:
    meta_path = Path(config["metadata_path"])
    lease_path = Path(config["lease_path"])
    now = _now()
    token = str(config["owner_token"])
    meta = _read_json(meta_path)
    if not meta or meta.get("owner_token") != token:
        return
    heartbeat = _stamp(now)
    expiry = _stamp(now + timedelta(seconds=LEASE_SECONDS)) if state in {"starting", "running"} else heartbeat
    owner_started_at = meta.get("owner_started_at") or heartbeat
    owner_pid_start_token = _proc_start_token(os.getpid())
    meta.update(
        {
            "status": state,
            "started_at": meta.get("started_at") or heartbeat,
            "owner_started_at": owner_started_at,
            "owner_pid_start_token": owner_pid_start_token,
            "pid": os.getpid(),
            "worker_pid": os.getpid(),
            "heartbeat_at": heartbeat,
            "lease_expires_at": expiry,
        }
    )
    if state in {"completed", "failed", "timed_out"}:
        meta["ended_at"] = meta.get("ended_at") or heartbeat
        meta["return_code"] = return_code
        meta.pop("reconciliation", None)
    lease = {
        "schema_version": 1,
        "job_id": config["job_id"],
        "session_id": config["session_id"],
        "profile": config["profile"],
        "owner_token": token,
        "owner_instance_id": config["owner_instance_id"],
        "owner_container": config["owner_container"],
        "owner_pid": os.getpid(),
        "owner_pid_start_token": owner_pid_start_token,
        "owner_started_at": owner_started_at,
        "state": state,
        "heartbeat_at": heartbeat,
        "lease_expires_at": expiry,
        "return_code": return_code,
    }
    # Publish the matching sidecar before marking the shared job as running.
    _atomic_json(lease_path, lease)
    _atomic_json(meta_path, meta)


def _kill_child(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def _parent_death_signal() -> None:
    """Ensure a CLI child does not outlive its lease-owning supervisor."""
    if os.name != "posix":
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
        if os.getppid() == 1:
            os.kill(os.getpid(), signal.SIGTERM)
    except (AttributeError, OSError):
        pass


def run(config: dict[str, Any], lock_fd: int) -> int:
    # The parent acquired this flock before spawning us and passed the open file
    # description across exec. It remains held even if the MCP server exits.
    if fcntl is None:
        return 125
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 125
    signal.signal(signal.SIGTERM, lambda *_: _shutdown.set())
    signal.signal(signal.SIGINT, lambda *_: _shutdown.set())

    _write_owner_state(config, "starting")
    output_path = Path(config["output_path"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    child: subprocess.Popen[Any] | None = None
    status = "failed"
    return_code: int | None = 127
    try:
        with output_path.open("wb") as output:
            kwargs: dict[str, Any] = {
                "stdout": output,
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL,
                "shell": False,
                "env": os.environ.copy(),
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            }
            if os.name == "posix":
                kwargs["start_new_session"] = True
                kwargs["preexec_fn"] = _parent_death_signal
            child = subprocess.Popen(config["command"], **kwargs)
            _write_owner_state(config, "running")
            deadline = time.monotonic() + max(1, int(config["timeout"]))
            while True:
                if _shutdown.is_set():
                    _kill_child(child)
                    return_code = child.poll()
                    status = "failed"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill_child(child)
                    return_code = child.poll()
                    status = "timed_out"
                    break
                try:
                    return_code = child.wait(timeout=min(HEARTBEAT_SECONDS, remaining))
                    status = "completed" if return_code == 0 else "failed"
                    break
                except subprocess.TimeoutExpired:
                    _write_owner_state(config, "running")
    except (OSError, ValueError):
        status = "failed"
        return_code = 127
        try:
            with output_path.open("ab") as output:
                output.write(b"Hermes session worker failed to launch.\n")
        except OSError:
            pass
    finally:
        _write_owner_state(config, status, return_code)
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        try:
            os.close(lock_fd)
        except OSError:
            pass
    return 0 if status == "completed" else (124 if status == "timed_out" else 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lease-fd", type=int, required=True)
    args = parser.parse_args()
    try:
        config = json.load(sys.stdin)
    except (ValueError, OSError):
        return 125
    if not isinstance(config, dict) or not isinstance(config.get("command"), list):
        return 125
    return run(config, args.lease_fd)


if __name__ == "__main__":
    raise SystemExit(main())
