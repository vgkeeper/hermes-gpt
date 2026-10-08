from __future__ import annotations

import server
import operator_session
import threading
import time


def test_api_status_result_and_wait(monkeypatch):
    calls = []
    class Client:
        def get_run(self, run_id, profile="default"):
            calls.append((run_id, profile))
            return {"run_id": run_id, "session_id": "sess", "profile": "wrong-server-value",
                    "status": "completed", "output": "official output"}
    monkeypatch.setattr(server, "HermesAPIClient", Client)
    monkeypatch.setattr(server, "_validate_session_profile", lambda profile="default": profile)
    status = server.hermes_session_job_status("run_abc")
    result = server.hermes_session_job_result("run_abc")
    waited = server.hermes_session_job_wait("run_abc", wait_seconds=0)
    profiled = server.hermes_session_job_status("run_abc", profile="research")
    profiled_result = server.hermes_session_job_result("run_abc", profile="research")
    profiled_wait = server.hermes_session_job_wait("run_abc", wait_seconds=0, profile="research")
    assert status["profile"] == "default"
    assert profiled["profile"] == profiled_result["profile"] == profiled_wait["profile"] == "research"
    assert calls == [("run_abc", "default")] * 3 + [("run_abc", "research")] * 3
    assert status["status"] == "completed" and status["return_code"] is None
    assert result["response"] == "official output"
    assert waited["status"] == "completed" and waited["await"]["timed_out"] is False


def test_legacy_job_ids_use_existing_read_paths(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_default_hermes_root", lambda: "/legacy-root")
    monkeypatch.setattr(operator_session, "hermes_session_job_status", lambda *args: calls.append(("status", args)) or {"legacy": True})
    monkeypatch.setattr(operator_session, "hermes_session_job_result", lambda *args: calls.append(("result", args)) or {"legacy": True})
    monkeypatch.setattr(operator_session, "hermes_session_job_wait", lambda *args: calls.append(("wait", args)) or {"legacy": True})
    assert server.hermes_session_job_status("a" * 32) == {"legacy": True}
    assert server.hermes_session_job_result("a" * 32) == {"legacy": True}
    assert server.hermes_session_job_wait("a" * 32, 1) == {"legacy": True}
    assert [item[0] for item in calls] == ["status", "result", "wait"]


def test_api_start_error_never_falls_back_to_local_subprocess(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(server, "_validate_session_profile", lambda profile="default": profile)
    class Client:
        def create_run(self, *args, **kwargs):
            raise server.HermesAPIError("network_error")
    monkeypatch.setattr(server, "HermesAPIClient", Client)
    monkeypatch.setattr(operator_session.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("legacy subprocess invoked")))
    result = server.hermes_session_continue("sess", "prompt")
    assert result["code"] == "HERMES_API_ERROR"


def test_api_create_error_after_session_creation_does_not_use_legacy(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(server, "_validate_session_profile", lambda profile="default": profile)
    class Client:
        def create_session(self, **kwargs):
            return {"session_id": "sess_created"}
        def create_run(self, *args, **kwargs):
            raise server.HermesAPIError("network_error")
    monkeypatch.setattr(server, "HermesAPIClient", Client)
    monkeypatch.setattr(operator_session.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("legacy subprocess invoked")))
    result = server.hermes_session_create("prompt")
    assert result["code"] == "HERMES_API_ERROR"


def test_create_extracts_nested_api_session_id(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(server, "_start_session_api_supervisor", lambda *args: True)
    monkeypatch.setattr(server, "_validate_session_profile", lambda profile="default": profile)
    calls = []
    class Client:
        def create_session(self, **kwargs):
            return {"object": "hermes.session.create", "session": {"id": "nested_sess"}}
        def create_run(self, prompt, *, session_id, profile):
            calls.append((session_id, profile))
            return {"run_id": "run_nested", "status": "started"}
    monkeypatch.setattr(server, "HermesAPIClient", Client)
    result = server.hermes_session_create("prompt")
    assert result["job_id"] == "run_nested"
    assert calls == [("nested_sess", "default")]


def test_create_and_continue_pass_validated_timeout_and_mission_to_supervisor(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(server, "_validate_session_profile", lambda profile="default": profile)
    monkeypatch.setattr(operator_session, "_validate_start", lambda sid, prompt, timeout, profile, mission:
                        (sid, prompt.strip(), timeout, profile, mission.strip()))
    monkeypatch.setattr(operator_session, "_validate_create", lambda prompt, timeout, profile, title, mission:
                        (prompt.strip(), timeout, profile, title.strip() if title else None, mission.strip()))
    starts = []
    def capture_start(*args):
        starts.append(args)
        server._release_session_api_reservation(args[3], args[2], args[6])
        return True
    monkeypatch.setattr(server, "_start_session_api_supervisor", capture_start)
    class Client:
        def create_session(self, **kwargs):
            return {"session_id": "created-session"}
        def create_run(self, prompt, *, session_id, profile):
            return {"run_id": "run_" + session_id, "status": "started"}
    monkeypatch.setattr(server, "HermesAPIClient", Client)

    continued = server.hermes_session_continue(
        "existing-session", " prompt ", max_job_runtime_seconds=37, profile="default", mission_id=" mission-1 "
    )
    created = server.hermes_session_create(
        " create prompt ", max_job_runtime_seconds=51, profile="default", mission_id=" mission-2 "
    )
    assert continued["success"] and created["success"]
    assert [(args[1], args[2], args[3], args[4], args[5]) for args in starts] == [
        ("run_existing-session", "existing-session", "default", 37, "mission-1"),
        ("run_created-session", "created-session", "default", 51, "mission-2"),
    ]


def _wait_for_supervisor_cleanup(run_id):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with server._SESSION_API_SUPERVISORS_LOCK:
            if run_id not in server._SESSION_API_SUPERVISORS:
                return
        time.sleep(0.01)
    raise AssertionError(f"supervisor {run_id} did not clean up")


def test_terminal_sse_checks_durable_run_and_publishes_exact_event(monkeypatch):
    published = []
    done = threading.Event()
    class Client:
        def __init__(self):
            self.get_calls = 0
        def iter_run_events(self, run_id, profile, *, stream_deadline_monotonic=None):
            yield {"event": "run.completed"}
        def get_run(self, run_id, profile):
            self.get_calls += 1
            return {"run_id": run_id, "session_id": "session-9", "status": "completed"}
    client = Client()
    def publish(**kwargs):
        published.append(kwargs)
        done.set()
    monkeypatch.setattr(server.op_live_events, "publish_event", publish)
    monkeypatch.setattr(server, "_default_hermes_root", lambda: "/hermes-root")
    server._start_session_api_supervisor(client, "run_terminal", "session-9", "work", 60, "mission-9")
    assert done.wait(2)
    _wait_for_supervisor_cleanup("run_terminal")
    event = published[0]
    assert client.get_calls == 1
    assert len(published) == 1
    assert {key: event[key] for key in (
        "topic", "kind", "subject_type", "subject_id", "mission_id", "source", "payload", "event_id"
    )} == {
        "topic": "session", "kind": "job.terminal", "subject_type": "job",
        "subject_id": "run_terminal", "mission_id": "mission-9", "source": "hermes-api-bridge",
        "payload": {"job_id": "run_terminal", "session_id": "session-9", "status": "completed", "profile": "work"},
        "event_id": "lev-session-job-run_terminal",
    }
    assert event["hermes_root"] == "/hermes-root"


def test_supervisor_skips_live_event_when_mission_is_empty(monkeypatch):
    published = []
    class Client:
        def iter_run_events(self, run_id, profile, *, stream_deadline_monotonic=None):
            yield {"event": "run.failed"}
        def get_run(self, run_id, profile):
            return {"run_id": run_id, "session_id": "session-1", "status": "failed"}
    monkeypatch.setattr(server.op_live_events, "publish_event", lambda **kwargs: published.append(kwargs))
    server._start_session_api_supervisor(Client(), "run_no_mission", "session-1", "default", 60, "")
    _wait_for_supervisor_cleanup("run_no_mission")
    assert published == []


def test_timer_stops_run_via_official_api_and_supervisor_cleans_up(monkeypatch):
    stopped = threading.Event()
    published = []
    class Client:
        def iter_run_events(self, run_id, profile, *, stream_deadline_monotonic=None):
            stopped.wait(2)
            if False:
                yield {}
        def stop_run(self, run_id, profile):
            assert (run_id, profile) == ("run_timeout", "profile-x")
            stopped.set()
        def get_run(self, run_id, profile):
            return {"run_id": run_id, "session_id": "session-x", "status": "cancelled"}
    monkeypatch.setattr(server.op_live_events, "publish_event", lambda **kwargs: published.append(kwargs))
    server._start_session_api_supervisor(Client(), "run_timeout", "session-x", "profile-x", 0.05, "mission-time")
    assert stopped.wait(2)
    _wait_for_supervisor_cleanup("run_timeout")
    assert published[0]["payload"]["timed_out"] is True


def test_sse_error_falls_back_to_one_durable_terminal_read_and_publishes(monkeypatch):
    published = []
    class Client:
        def __init__(self):
            self.get_calls = 0
        def iter_run_events(self, run_id, profile, *, stream_deadline_monotonic=None):
            raise server.HermesAPIError("http_error", 404)
            yield {}
        def get_run(self, run_id, profile):
            self.get_calls += 1
            return {"run_id": run_id, "session_id": "session-2", "status": "interrupted"}
    client = Client()
    monkeypatch.setattr(server.op_live_events, "publish_event", lambda **kwargs: published.append(kwargs))
    server._start_session_api_supervisor(client, "run_sse_error", "session-2", "default", 60, "mission-2")
    _wait_for_supervisor_cleanup("run_sse_error")
    assert len(published) == 1
    assert client.get_calls == 1
    assert published[0]["payload"] == {
        "job_id": "run_sse_error", "session_id": "session-2", "status": "interrupted", "profile": "default"
    }


def test_supervision_never_starts_local_subprocess(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(server, "_validate_session_profile", lambda profile="default": profile)
    monkeypatch.setattr(operator_session, "_validate_start", lambda sid, prompt, timeout, profile, mission:
                        (sid, prompt, timeout, profile, mission))
    monkeypatch.setattr(server, "_start_session_api_supervisor", lambda *args: True)
    monkeypatch.setattr(operator_session.subprocess, "Popen", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("local subprocess invoked")))
    class Client:
        def create_run(self, *args, **kwargs):
            return {"run_id": "run_no_process", "status": "started"}
    monkeypatch.setattr(server, "HermesAPIClient", Client)
    assert server.hermes_session_continue("session", "prompt")["success"]


def test_create_invalid_title_is_rejected_before_api(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_CONTROL_ENV, "1")
    calls = []
    class Client:
        def create_session(self, **kwargs):
            calls.append(kwargs)
            raise AssertionError("API must not be called for invalid title")
    monkeypatch.setattr(server, "HermesAPIClient", Client)
    result = server.hermes_session_create("prompt", title="   ")
    assert result["code"] == "INVALID_TITLE"
    assert calls == []


def test_terminal_observed_before_timer_prevents_timeout_stop(monkeypatch):
    stopped = []
    state = {
        "state_lock": threading.Lock(), "terminal_observed": False,
        "finished": False, "stop_started": False, "timed_out": False,
        "mission_id": "", "session_id": "session-race", "profile": "default",
        "run_id": "run_race", "stop_finished": threading.Event(), "client": type("Client", (), {
            "stop_run": lambda self, *args, **kwargs: stopped.append(args)
        })(), "run_id": "run_race", "profile": "default",
    }
    server._finish_session_api_supervisor(
        state, {"run_id": "run_race", "status": "completed"}
    )
    server._session_api_timeout(state)
    assert state["terminal_observed"] and state["finished"]
    assert state["timed_out"] is False
    assert stopped == []


def test_timeout_stops_once_and_releases_session_ownership(monkeypatch):
    stopped = threading.Event()
    calls = []
    class Client:
        def iter_run_events(self, run_id, profile, *, stream_deadline_monotonic=None):
            raise server.HermesAPIError("http_error", 404)
            yield {}
        def stop_run(self, run_id, profile):
            calls.append((run_id, profile))
            stopped.set()
        def get_run(self, run_id, profile):
            return {"run_id": run_id, "session_id": "session-once",
                    "status": "cancelled" if stopped.is_set() else "running"}
    monkeypatch.setattr(server.op_live_events, "publish_event", lambda **kwargs: None)
    assert server._start_session_api_supervisor(
        Client(), "run_timeout_once", "session-once", "default", 0.05, ""
    )
    assert stopped.wait(2)
    _wait_for_supervisor_cleanup("run_timeout_once")
    assert calls == [("run_timeout_once", "default")]
    assert ("default", "session-once") not in server._SESSION_API_SESSION_OWNERS


def test_session_busy_prevents_second_run_and_terminal_releases_ownership(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(server, "_validate_session_profile", lambda profile="default": profile)
    terminal = threading.Event()
    class Client:
        def __init__(self):
            self.create_calls = 0
        def create_run(self, prompt, *, session_id, profile):
            self.create_calls += 1
            return {"run_id": f"run_serial_{self.create_calls}", "status": "running"}
        def iter_run_events(self, run_id, profile, *, stream_deadline_monotonic=None):
            if run_id == "run_serial_1":
                terminal.wait(2)
            yield {"event": "run.completed"}
        def get_run(self, run_id, profile):
            return {"run_id": run_id, "session_id": "serial-session", "status": "completed"}
    client = Client()
    monkeypatch.setattr(server, "HermesAPIClient", lambda: client)
    first = server.hermes_session_continue("serial-session", "first")
    second = server.hermes_session_continue("serial-session", "second")
    assert first["success"]
    assert second["code"] == "SESSION_BUSY"
    assert client.create_calls == 1
    terminal.set()
    _wait_for_supervisor_cleanup(first["job_id"])
    assert ("default", "serial-session") not in server._SESSION_API_SESSION_OWNERS


def test_create_passes_validated_safe_title_to_api(monkeypatch):
    monkeypatch.setenv(server.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(server, "_start_session_api_supervisor", lambda *args: True)
    captured = []
    class Client:
        def create_session(self, **kwargs):
            captured.append(kwargs)
            return {"session_id": "safe-title-session"}
        def create_run(self, prompt, *, session_id, profile):
            return {"run_id": "run_safe_title", "status": "running"}
    monkeypatch.setattr(server, "HermesAPIClient", Client)
    result = server.hermes_session_create("prompt", title="  New title  ")
    assert result["success"]
    assert captured[0]["title"] == "New title"


def test_timer_start_failure_rolls_back_registry_and_session_owner(monkeypatch):
    def fail_start(self):
        raise RuntimeError("timer start failed")
    monkeypatch.setattr(server.threading.Timer, "start", fail_start)
    class Client:
        pass
    result = server._start_session_api_supervisor(
        Client(), "run_start_fail", "session-start-fail", "default", 3600, ""
    )
    assert result is False
    with server._SESSION_API_SUPERVISORS_LOCK:
        assert "run_start_fail" not in server._SESSION_API_SUPERVISORS
        assert ("default", "session-start-fail") not in server._SESSION_API_SESSION_OWNERS


def test_supervisor_thread_start_failure_rolls_back_timer_and_ownership(monkeypatch):
    original_start = server.threading.Thread.start
    def fail_supervisor_thread(self):
        if self.name.startswith("hermes-api-run-"):
            raise RuntimeError("supervisor thread start failed")
        return original_start(self)
    monkeypatch.setattr(server.threading.Thread, "start", fail_supervisor_thread)
    class Client:
        pass
    assert not server._start_session_api_supervisor(
        Client(), "run_thread_start_fail", "session-thread-start-fail", "default", 3600, ""
    )
    with server._SESSION_API_SUPERVISORS_LOCK:
        assert "run_thread_start_fail" not in server._SESSION_API_SUPERVISORS
        assert ("default", "session-thread-start-fail") not in server._SESSION_API_SESSION_OWNERS


def test_supervisor_start_failure_stops_run_and_releases_reservation(monkeypatch):
    monkeypatch.setattr(server, "_start_session_api_supervisor", lambda *args: False)
    reservation = server._reserve_session_api_session("profile-stop", "session-stop")
    assert reservation is not None
    stop_calls = []
    class Client:
        def stop_run(self, run_id, *, profile):
            stop_calls.append((run_id, profile))
    result = server._session_api_started(
        {"run_id": "run_stop_failed_supervisor"}, "session-stop", "profile-stop",
        Client(), 60, "", reservation,
    )
    assert result["code"] == "SUPERVISOR_START_FAILED"
    assert stop_calls == [("run_stop_failed_supervisor", "profile-stop")]
    with server._SESSION_API_SUPERVISORS_LOCK:
        assert ("profile-stop", "session-stop") not in server._SESSION_API_SESSION_OWNERS


def test_supervisor_start_failure_stop_error_preserves_error_code(monkeypatch):
    monkeypatch.setattr(server, "_start_session_api_supervisor", lambda *args: False)
    reservation = server._reserve_session_api_session("profile-stop-error", "session-stop-error")
    assert reservation is not None
    stop_calls = []
    class Client:
        def stop_run(self, run_id, *, profile):
            stop_calls.append((run_id, profile))
            raise server.HermesAPIError("network_error")
    result = server._session_api_started(
        {"run_id": "run_stop_error"}, "session-stop-error", "profile-stop-error",
        Client(), 60, "", reservation,
    )
    assert result["code"] == "SUPERVISOR_START_FAILED"
    assert stop_calls == [("run_stop_error", "profile-stop-error")]
    with server._SESSION_API_SUPERVISORS_LOCK:
        assert ("profile-stop-error", "session-stop-error") not in server._SESSION_API_SESSION_OWNERS
