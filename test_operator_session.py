import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import operator_live_events as live_events
import operator_policy as op
import operator_session as session
import operator_session_worker as worker
import server


class _ImmediateThread:
    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


class _FakeInput:
    def __init__(self):
        self.parts = []

    def write(self, value):
        self.parts.append(value)

    def close(self):
        pass

    def getvalue(self):
        return "".join(self.parts)


class _FakeProcess:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.stdin = _FakeInput()
        self.pid = 4321
        self.returncode = None

    def wait(self, timeout=None):
        self.kwargs["stdout"].write("mock Hermes response token=secret-value-123456789")
        self.kwargs["stdout"].flush()
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode


def test_session_control_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(session.ENABLE_SESSION_CONTROL_ENV, raising=False)
    result = session.hermes_session_continue("session-1", "hello", hermes_root=tmp_path)
    assert result["success"] is False
    assert result["code"] == "SESSION_CONTROL_DISABLED"


def test_mocked_continue_status_and_result(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    monkeypatch.setattr(session.threading, "Thread", _ImmediateThread)
    calls = []

    def fake_popen(argv, **kwargs):
        proc = _FakeProcess(argv, **kwargs)
        calls.append(proc)
        return proc

    monkeypatch.setattr(session.subprocess, "Popen", fake_popen)
    prompt = "private follow-up prompt"
    started = session.hermes_session_continue(
        "20260810_143227_6b0982",
        prompt,
        max_job_runtime_seconds=99999,
        hermes_root=tmp_path,
        agent_root=tmp_path / "agent",
        profile="project-manager",
        mission_id="msn-session-test",
    )
    assert started["success"] is True
    assert len(calls) == 1
    assert Path(calls[0].argv[1]).name == "operator_session_worker.py"
    worker_config = json.loads(calls[0].stdin.getvalue())
    assert Path(worker_config["command"][0]).name.lower() in {"hermes", "hermes.exe"}
    assert worker_config["command"][1:] == ["--resume", "20260810_143227_6b0982", "--oneshot", prompt]
    assert calls[0].kwargs["shell"] is False
    assert calls[0].kwargs["env"]["HERMES_PROFILE"] == "project-manager"
    assert calls[0].kwargs["env"]["HERMES_HOME"] == str(tmp_path / "profiles" / "project-manager")

    status = session.hermes_session_job_status(started["job_id"], tmp_path)
    assert status["job"]["status"] == "completed"
    assert status["job"]["timeout"] == session.MAX_JOB_RUNTIME_SECONDS
    assert status["job"]["max_job_runtime_seconds"] == session.MAX_JOB_RUNTIME_SECONDS
    assert status["job"]["profile"] == "project-manager"
    assert status["job"]["mission_id"] == "msn-session-test"
    event_result = json.loads(live_events.hermes_live_events_since(0, mission_id="msn-session-test", hermes_root=tmp_path))
    assert len(event_result["events"]) == 1
    assert event_result["events"][0]["kind"] == "job.terminal"
    assert event_result["events"][0]["payload"]["job_id"] == started["job_id"]
    assert prompt not in json.dumps(event_result["events"][0]["payload"])
    metadata_text = json.dumps(status)
    assert prompt not in metadata_text
    assert status["job"]["prompt_len"] == len(prompt)

    result = session.hermes_session_job_result(started["job_id"], 500, tmp_path)
    assert result["status"] == "completed"
    assert result["return_code"] == 0
    assert "secret-value" not in result["response"]
    assert "[REDACTED]" in result["response"]


def test_job_lookup_and_input_bounds(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    assert session.hermes_session_job_status("not-a-job", tmp_path)["code"] == "JOB_NOT_FOUND"
    assert session.hermes_session_continue("s", "", hermes_root=tmp_path)["code"] == "INVALID_PROMPT"
    assert session.hermes_session_continue(
        "s", "x" * (session.MAX_PROMPT_CHARS + 1), hermes_root=tmp_path
    )["code"] == "PROMPT_TOO_LARGE"
    assert session.hermes_session_continue("s", "x", max_job_runtime_seconds=True, hermes_root=tmp_path)["code"] == "INVALID_MAX_JOB_RUNTIME_SECONDS"


def test_same_session_cannot_run_concurrently(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setitem(session._active_sessions, "default:session-1", "b" * 32)
    result = session.hermes_session_continue("session-1", "next", hermes_root=tmp_path)
    assert result["code"] == "SESSION_BUSY"


def test_reconcile_marks_unowned_running_job_orphaned(monkeypatch, tmp_path):
    monkeypatch.delenv(session.SESSION_CONTROL_SHARED_STATE_ENV, raising=False)
    job_id = "a" * 32
    session._save({"job_id": job_id, "session_id": "s", "status": "running"}, tmp_path)
    result = session.hermes_session_job_status(job_id, tmp_path)
    assert result["job"]["status"] == "orphaned"
    assert "ownership" in result["job"]["reconciliation"]


def test_shared_state_preserves_recent_foreign_job_for_read_only_status_and_result(monkeypatch, tmp_path):
    monkeypatch.setenv(session.SESSION_CONTROL_SHARED_STATE_ENV, "1")
    job_id = "b" * 32
    session._save(
        {
            "job_id": job_id,
            "session_id": "shared-session",
            "profile": "default",
            "status": "running",
            "started_at": session._now(),
            "timeout": session.MAX_JOB_RUNTIME_SECONDS,
            "max_job_runtime_seconds": session.MAX_JOB_RUNTIME_SECONDS,
            "pid": 987654,
        },
        tmp_path,
    )

    status = session.hermes_session_job_status(job_id, tmp_path)
    result = session.hermes_session_job_result(job_id, hermes_root=tmp_path)

    assert status["job"]["status"] == "running"
    assert status["job"]["process_visibility"] == "external_pid_namespace"
    assert result["status"] == "running"
    assert result["process_visibility"] == "external_pid_namespace"
    persisted = session._load(job_id, tmp_path)
    assert persisted is not None
    assert persisted["status"] == "running"
    assert "reconciliation" not in persisted


def test_shared_state_times_out_foreign_job_only_after_recorded_max_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv(session.SESSION_CONTROL_SHARED_STATE_ENV, "1")
    job_id = "c" * 32
    started = datetime.now(timezone.utc) - timedelta(seconds=61)
    session._save(
        {
            "job_id": job_id,
            "session_id": "stale-shared-session",
            "profile": "default",
            "status": "running",
            "started_at": started.isoformat(),
            "timeout": 60,
            "max_job_runtime_seconds": 60,
            "pid": 987655,
        },
        tmp_path,
    )

    status = session.hermes_session_job_status(job_id, tmp_path)

    assert status["job"]["status"] == "timed_out"
    assert "maximum runtime elapsed" in status["job"]["reconciliation"]


def test_shared_state_rejects_parallel_continue_for_foreign_active_job(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setenv(session.SESSION_CONTROL_SHARED_STATE_ENV, "1")
    monkeypatch.setenv("HERMES_GPT_OPERATOR_ALLOWED_PROFILES", "default")
    job_id = "d" * 32
    session._save(
        {
            "job_id": job_id,
            "session_id": "busy-shared-session",
            "profile": "default",
            "status": "running",
            "started_at": session._now(),
            "timeout": session.MAX_JOB_RUNTIME_SECONDS,
            "max_job_runtime_seconds": session.MAX_JOB_RUNTIME_SECONDS,
            "pid": 987656,
        },
        tmp_path,
    )
    def unexpected_start(*_args, **_kwargs):
        raise AssertionError("shared-state conflict must not start another Hermes process")

    monkeypatch.setattr(session.subprocess, "Popen", unexpected_start)

    result = session.hermes_session_continue("busy-shared-session", "short safe turn", hermes_root=tmp_path)

    assert result["success"] is False
    assert result["code"] == "SESSION_BUSY"
    assert job_id in json.dumps(result)
    persisted = session._load(job_id, tmp_path)
    assert persisted is not None and persisted["status"] == "running"


def test_job_wait_returns_early_on_terminal_state(tmp_path):
    job_id = "c" * 32
    session._save(
        {"job_id": job_id, "session_id": "s", "profile": "dev", "status": "completed", "return_code": 0},
        tmp_path,
    )
    result = session.hermes_session_job_wait(job_id, wait_seconds=5, hermes_root=tmp_path)
    assert result["success"] is True
    assert result["status"] == "completed"
    assert result["return_code"] == 0
    assert result["await"]["timed_out"] is False


def test_job_wait_times_out_on_nonterminal_state(monkeypatch, tmp_path):
    job_id = "d" * 32
    monkeypatch.setattr(session, "_reconcile", lambda *a, **k: None)
    terminated = []
    monkeypatch.setattr(session, "_terminate", lambda proc: terminated.append(proc))
    session._save({"job_id": job_id, "session_id": "s", "status": "running"}, tmp_path)
    result = session.hermes_session_job_wait(job_id, wait_seconds=0, hermes_root=tmp_path)
    assert result["success"] is True
    assert result["status"] == "running"
    assert result["await"]["timed_out"] is True
    assert session._load(job_id, tmp_path)["status"] == "running"
    queried = session.hermes_session_job_status(job_id, tmp_path)
    assert queried["success"] is True
    assert queried["job"]["status"] == "running"
    assert terminated == []


def test_job_wait_clamps_seconds(tmp_path):
    job_id = "e" * 32
    session._save({"job_id": job_id, "session_id": "s", "status": "failed"}, tmp_path)
    result = session.hermes_session_job_wait(job_id, wait_seconds=99999, hermes_root=tmp_path)
    assert result["status"] == "failed"
    assert result["await"]["wait_seconds"] == session.MAX_JOB_WAIT_SECONDS

    result_bad = session.hermes_session_job_wait(job_id, wait_seconds="junk", hermes_root=tmp_path)
    assert result_bad["status"] == "failed"
    assert result_bad["await"]["wait_seconds"] == session.MAX_JOB_WAIT_SECONDS


def test_job_wait_missing_job(tmp_path):
    result = session.hermes_session_job_wait("f" * 32, wait_seconds=0, hermes_root=tmp_path)
    assert result["code"] == "JOB_NOT_FOUND"


# ---------------------------------------------------------------------------
# hermes_session_create — validation cases
# ---------------------------------------------------------------------------


class _FakeSessionDB:
    """In-memory stand-in for hermes_state.SessionDB write surface."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.created = []
        self.titled = []

    def create_session(self, session_id, source="cli", **kwargs):
        self.created.append((session_id, source))
        return session_id

    def set_session_title(self, session_id, title):
        self.titled.append((session_id, title))
        return True

    def close(self):
        pass


def test_session_create_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(session.ENABLE_SESSION_CONTROL_ENV, raising=False)
    result = session.hermes_session_create("new session prompt", hermes_root=tmp_path)
    assert result["success"] is False
    assert result["code"] == "SESSION_CONTROL_DISABLED"


def test_session_create_requires_nonempty_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    assert session.hermes_session_create("", hermes_root=tmp_path)["code"] == "INVALID_PROMPT"
    assert session.hermes_session_create("   ", hermes_root=tmp_path)["code"] == "INVALID_PROMPT"


def test_session_create_prompt_size_bounds(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    result = session.hermes_session_create(
        "x" * (session.MAX_PROMPT_CHARS + 1), hermes_root=tmp_path
    )
    assert result["code"] == "PROMPT_TOO_LARGE"


def test_session_create_rejects_bool_or_junk_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    assert session.hermes_session_create(
        "p", max_job_runtime_seconds=True, hermes_root=tmp_path
    )["code"] == "INVALID_MAX_JOB_RUNTIME_SECONDS"


def test_session_create_rejects_invalid_profile(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    result = session.hermes_session_create("p", profile="bad:profile", hermes_root=tmp_path)
    assert result["code"] == "INVALID_PROFILE"


def test_session_create_rejects_overlong_title(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    result = session.hermes_session_create(
        "p", title="t" * (session.MAX_SESSION_TITLE_CHARS + 1), hermes_root=tmp_path
    )
    assert result["code"] == "INVALID_TITLE"


# ---------------------------------------------------------------------------
# hermes_session_create — distinct session + async shape
# ---------------------------------------------------------------------------


def test_session_create_builds_new_distinct_session(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(session.threading, "Thread", _ImmediateThread)
    fake_db = _FakeSessionDB()
    monkeypatch.setattr(session, "SessionDB", lambda **kw: fake_db)
    calls = []

    def fake_popen(argv, **kwargs):
        proc = _FakeProcess(argv, **kwargs)
        calls.append(proc)
        return proc

    monkeypatch.setattr(session.subprocess, "Popen", fake_popen)
    prompt = "first work of the new session"
    started = session.hermes_session_create(
        prompt,
        max_job_runtime_seconds=7200,
        hermes_root=tmp_path,
        agent_root=tmp_path / "agent",
        profile="project-manager",
        title="My fresh session",
        mission_id="msn-created-session",
    )
    # async shape
    assert started["success"] is True
    assert set(started) >= {"job_id", "session_id", "profile", "status"}
    assert started["profile"] == "project-manager"
    assert started["status"] == "running"
    assert session._load(started["job_id"], tmp_path)["max_job_runtime_seconds"] == 7200
    assert session._load(started["job_id"], tmp_path)["timeout"] == 7200
    assert session._load(started["job_id"], tmp_path)["mission_id"] == "msn-created-session"
    # a new session was created in the DB, distinct from any caller-supplied id
    assert len(fake_db.created) == 1
    new_sid, source = fake_db.created[0]
    assert new_sid == started["session_id"]
    assert source == session.SESSION_CREATE_SOURCE
    # distinct session id pattern (ts_uuid6)
    import re as _re
    assert _re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{6}", new_sid)
    # title recorded
    assert fake_db.titled == [(new_sid, "My fresh session")]
    # The durable worker keeps the Hermes CLI argv in its private stdin payload.
    worker_config = json.loads(calls[0].stdin.getvalue())
    assert worker_config["command"][1:] == ["--resume", new_sid, "--oneshot", prompt]

    # job tracked to completion and readable by wait/result
    waited = session.hermes_session_job_wait(started["job_id"], 5, tmp_path)
    assert waited["success"] is True
    result = session.hermes_session_job_result(started["job_id"], 500, tmp_path)
    assert result["status"] == "completed"
    assert "secret-value" not in result["response"]


def test_session_create_does_not_accept_arbitrary_profile(monkeypatch, tmp_path):
    # profile is restricted (validated) the same way hermes_session_continue is
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(session, "SessionDB", lambda **kw: _FakeSessionDB())
    result = session.hermes_session_create(
        "p", profile="../../etc/passwd", hermes_root=tmp_path
    )
    assert result["code"] == "INVALID_PROFILE"


def _lease_record(job_id, session_id, token, *, expiry, state="running"):
    return {
        "schema_version": 1,
        "job_id": job_id,
        "session_id": session_id,
        "profile": "default",
        "owner_token": token,
        "owner_instance_id": "test-owner",
        "owner_container": "test-gateway",
        "owner_pid": 999999,
        "state": state,
        "heartbeat_at": session._now(),
        "lease_expires_at": expiry,
    }


def test_live_shared_lease_recovers_legacy_orphan_without_pid_visibility(monkeypatch, tmp_path):
    monkeypatch.delenv(session.SESSION_CONTROL_SHARED_STATE_ENV, raising=False)
    job_id, session_id, token = "f" * 32, "namespace-session", "owner-token"
    lock_path, lease_path = session._session_lease_paths(session_id, "default", tmp_path)
    busy, lock_fd = session._try_session_lock(lock_path)
    assert not busy and lock_fd is not None
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    lease_path.write_text(json.dumps(_lease_record(job_id, session_id, token, expiry=expiry)), encoding="utf-8")
    session._save(
        {
            "job_id": job_id,
            "session_id": session_id,
            "profile": "default",
            "status": "orphaned",
            "owner_token": token,
            "pid": 999999,
            "reconciliation": "server restarted; process ownership could not be proven",
        },
        tmp_path,
    )
    try:
        status = session.hermes_session_job_status(job_id, tmp_path)
        result = session.hermes_session_job_result(job_id, hermes_root=tmp_path)
        assert status["job"]["status"] == "running"
        assert status["job"]["process_visibility"] == "external_pid_namespace"
        assert result["status"] == "running"
        assert "owner-token" not in json.dumps(status)
        persisted = session._load(job_id, tmp_path)
        assert persisted["status"] == "running"
        assert "reconciliation" not in persisted
    finally:
        session.fcntl.flock(lock_fd, session.fcntl.LOCK_UN)
        os.close(lock_fd)


def test_expired_lease_is_preserved_during_grace_then_orphaned(monkeypatch, tmp_path):
    monkeypatch.delenv(session.SESSION_CONTROL_SHARED_STATE_ENV, raising=False)
    job_id, session_id, token = "e" * 32, "lease-grace-session", "grace-owner"
    _, lease_path = session._session_lease_paths(session_id, "default", tmp_path)
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    future = (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()
    lease_path.write_text(json.dumps(_lease_record(job_id, session_id, token, expiry=future)), encoding="utf-8")
    session._save(
        {"job_id": job_id, "session_id": session_id, "profile": "default", "status": "running", "owner_token": token},
        tmp_path,
    )
    assert session.hermes_session_job_status(job_id, tmp_path)["job"]["status"] == "running"

    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    lease_path.write_text(json.dumps(_lease_record(job_id, session_id, token, expiry=expired)), encoding="utf-8")
    status = session.hermes_session_job_status(job_id, tmp_path)
    assert status["job"]["status"] == "orphaned"
    assert "lease expired" in status["job"]["reconciliation"]


def _fake_hermes_executable(tmp_path, delay=1.5, name="hermes-fake"):
    executable = tmp_path / name
    executable.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(" + repr(delay) + ")\nprint('MCP_GATEWAY_E2E_OK')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _second_runtime_read(job_id, root):
    script = (
        "import json,os,sys; import operator_session as s; from pathlib import Path; "
        "os.kill=lambda *_args: (_ for _ in ()).throw(ProcessLookupError('isolated PID namespace')); "
        "r=Path(sys.argv[2]); a=s.hermes_session_job_status(sys.argv[1],r); "
        "b=s.hermes_session_job_result(sys.argv[1],hermes_root=r); "
        "print(json.dumps({'status':a.get('job',{}).get('status'),"
        "'visibility':a.get('job',{}).get('process_visibility'),"
        "'result_status':b.get('status'),'response':b.get('response')}))"
    )
    env = os.environ.copy()
    env["HERMES_GPT_SESSION_CONTROL_SHARED_STATE"] = "0"
    env["PYTHONPATH"] = str(Path(session.__file__).resolve().parent)
    completed = subprocess.run(
        [sys.executable, "-c", script, job_id, str(root)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        env=env,
    )
    return json.loads(completed.stdout)


def _second_runtime_continue(session_id, prompt, root, executable_dir):
    script = (
        "import json,sys; from pathlib import Path; import operator_session as s; "
        "print(json.dumps(s.hermes_session_continue(sys.argv[1],sys.argv[2],"
        "max_job_runtime_seconds=20,hermes_root=Path(sys.argv[3]))))"
    )
    env = os.environ.copy()
    env["HERMES_GPT_ENABLE_SESSION_CONTROL"] = "1"
    env["HERMES_GPT_SESSION_CONTROL_SHARED_STATE"] = "0"
    env["HERMES_HOME"] = str(root)
    env["PYTHONPATH"] = str(Path(session.__file__).resolve().parent)
    env["PATH"] = str(executable_dir) + os.pathsep + env.get("PATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", script, session_id, prompt, str(root)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        env=env,
    )
    return json.loads(completed.stdout)


def test_detached_worker_survives_parent_handle_loss_and_two_runtime_reads(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.delenv(session.SESSION_CONTROL_SHARED_STATE_ENV, raising=False)
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    _fake_hermes_executable(executable_dir, delay=3.0, name="hermes")
    manager_script = (
        "import json,operator_session as s; "
        "print(json.dumps(s.hermes_session_continue('namespace-e2e-session','safe test',"
        "max_job_runtime_seconds=20)),flush=True)"
    )
    env = os.environ.copy()
    env["HERMES_GPT_ENABLE_SESSION_CONTROL"] = "1"
    env["HERMES_GPT_SESSION_CONTROL_SHARED_STATE"] = "0"
    env["HERMES_HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(session.__file__).resolve().parent)
    env["PATH"] = str(executable_dir) + os.pathsep + env.get("PATH", "")
    manager = subprocess.run(
        [sys.executable, "-c", manager_script],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        env=env,
    )
    started = json.loads(manager.stdout)
    assert started["success"] is True
    job_id = started["job_id"]

    # The MCP-like parent process has exited; only the detached lease owner remains.
    observed = ""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        observed = session.hermes_session_job_status(job_id, tmp_path)["job"]["status"]
        if observed == "running":
            break
        time.sleep(0.05)
    assert observed == "running"
    owner = session._load(job_id, tmp_path)
    assert owner is not None
    assert owner.get("owner_token")
    assert owner.get("owner_instance_id")
    assert owner.get("owner_container")
    assert owner.get("owner_pid_start_token")
    assert owner.get("heartbeat_at")
    assert owner.get("lease_expires_at")
    foreign = {}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        foreign = _second_runtime_read(job_id, tmp_path)
        if foreign["status"] == "running" and foreign["result_status"] == "running":
            break
        time.sleep(0.05)
    assert foreign["status"] == "running"
    assert foreign["result_status"] == "running"
    assert foreign["visibility"] == "external_pid_namespace"

    waited = session.hermes_session_job_wait(job_id, wait_seconds=8, hermes_root=tmp_path)
    assert waited["status"] == "completed"
    result = session.hermes_session_job_result(job_id, hermes_root=tmp_path)
    assert result["response"].strip() == "MCP_GATEWAY_E2E_OK"
    terminal_from_second = _second_runtime_read(job_id, tmp_path)
    assert terminal_from_second["status"] == "completed"
    assert terminal_from_second["result_status"] == "completed"
    assert terminal_from_second["response"].strip() == "MCP_GATEWAY_E2E_OK"


def test_shared_file_lock_allows_only_one_concurrent_continue(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.delenv(session.SESSION_CONTROL_SHARED_STATE_ENV, raising=False)
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    fake_hermes = _fake_hermes_executable(executable_dir, delay=1.5, name="hermes")
    monkeypatch.setattr(session, "_hermes_executable", lambda _root=None: str(fake_hermes))
    first = session.hermes_session_continue(
        "one-owner-session", "first safe turn", max_job_runtime_seconds=20, hermes_root=tmp_path
    )
    assert first["success"] is True
    second = _second_runtime_continue("one-owner-session", "second safe turn", tmp_path, executable_dir)
    assert second["success"] is False
    assert second["code"] == "SESSION_BUSY"
    files = [
        session._load(path.stem, tmp_path)
        for path in session._root(tmp_path).glob("*.json")
        if (session._load(path.stem, tmp_path) or {}).get("session_id") == "one-owner-session"
    ]
    assert len(files) == 1
    assert session.hermes_session_job_wait(first["job_id"], wait_seconds=8, hermes_root=tmp_path)["status"] == "completed"


def test_mission_id_validation_and_tool_surface_compatibility(monkeypatch, tmp_path):
    import inspect

    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    invalid = session.hermes_session_continue(
        "session-safe", "turn", mission_id="bad mission", hermes_root=tmp_path
    )
    assert invalid["code"] == "INVALID_MISSION_ID"
    for tool in (
        server.hermes_session_continue,
        server.hermes_session_send,
        server.hermes_session_create,
    ):
        assert inspect.signature(tool).parameters["mission_id"].default == ""


def test_worker_terminal_event_waits_for_persisted_state_and_reconciles_idempotently(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    hermes_root = tmp_path / "hermes"
    job_id = "a" * 32
    mission_id = "msn-worker-wakeup"
    metadata_path, _ = session._paths(job_id, hermes_root)
    lease_path = session._session_lease_paths("session-wakeup", "default", hermes_root)[
        1
    ]
    session._save(
        {
            "job_id": job_id,
            "session_id": "session-wakeup",
            "profile": "default",
            "mission_id": mission_id,
            "status": "running",
            "return_code": None,
            "owner_token": "owner-test",
            "prompt_len": 29,
            "prompt_sha256": "not-a-prompt",
        },
        hermes_root,
    )
    config = {
        "job_id": job_id,
        "metadata_path": str(metadata_path),
        "session_id": "session-wakeup",
        "profile": "default",
        "owner_token": "owner-test",
        "owner_instance_id": "test-instance",
        "owner_container": "test-container",
        "lease_path": str(lease_path),
    }

    worker._write_owner_state(config, "running")
    before_terminal = json.loads(
        live_events.hermes_live_events_since(0, hermes_root=hermes_root)
    )
    assert before_terminal["events"] == []

    real_publish = live_events.publish_event
    attempts = []

    def fail_once_after_durable_terminal(**kwargs):
        durable = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert durable["status"] == "completed"
        attempts.append(kwargs)
        raise OSError("temporary event store failure")

    monkeypatch.setattr(live_events, "publish_event", fail_once_after_durable_terminal)
    worker._write_owner_state(config, "completed", 0)
    persisted = session._load(job_id, hermes_root)
    assert persisted["mission_id"] == mission_id
    assert persisted["status"] == "completed"
    assert len(attempts) == 1

    monkeypatch.setattr(live_events, "publish_event", real_publish)
    session._reconcile(hermes_root)  # a restarted reader retries from durable metadata
    session._reconcile(hermes_root)  # repeated observations remain idempotent
    result = json.loads(
        live_events.hermes_live_events_since(
            0, mission_id=mission_id, hermes_root=hermes_root
        )
    )
    assert len(result["events"]) == 1
    event = result["events"][0]
    assert (event["topic"], event["kind"], event["subject_type"]) == (
        "session",
        "job.terminal",
        "job",
    )
    assert event["subject_id"] == job_id
    assert event["source"] == "session-runtime"
    assert event["event_id"] == f"lev-session-job-{job_id}"
    assert event["payload"] == {
        "job_id": job_id,
        "session_id": "session-wakeup",
        "status": "completed",
        "return_code": 0,
    }
    assert "prompt" not in json.dumps(event["payload"]).lower()
    assert "not-a-prompt" not in json.dumps(event["payload"])



def test_legacy_terminal_job_without_mission_id_emits_no_event(monkeypatch, tmp_path):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    job_id = "b" * 32
    session._save(
        {"job_id": job_id, "session_id": "legacy-session", "status": "completed", "return_code": 0},
        tmp_path,
    )
    session._reconcile(tmp_path)
    events = json.loads(live_events.hermes_live_events_since(0, hermes_root=tmp_path))
    assert events["events"] == []
