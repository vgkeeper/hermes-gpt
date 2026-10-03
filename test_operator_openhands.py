from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import operator_contract as contracts
import operator_failure_semantics as failure_semantics
import operator_policy as policy
import operator_runners as runners
from operator_openhands import HttpRuntime, OpenHandsBackend


class FakeRuntime:
    def __init__(self):
        self.conversation_result = {"execution_status": "RUNNING"}
        self.task = {"status": "READY", "app_conversation_id": "conversation-1"}
        self.started = []

    def start(self, **kwargs):
        self.started.append(kwargs)
        return {"id": "start-1", "status": "PENDING"}

    def start_task(self, **kwargs):
        return self.task

    def conversation(self, **kwargs):
        return self.conversation_result


def _contract(root: Path):
    return {
        "schema": "hermes.work-contract/v1",
        "task_id": "openhands-task-001",
        "assigned_agent": "coder",
        "assigned_profile": "default",
        "objective": "Implement a small, bounded code change.",
        "allowed_scope": {"workspaces": [str(root)], "profiles": ["default"]},
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
        "authorization": {"class": "reversible_write", "approved": True, "approved_by": "owner", "approval_reference": "test"},
        "execution": {"backend": "openhands", "options": {"repository": "owner/repo", "branch": "task-branch"}},
    }


def _enable(monkeypatch, root: Path):
    monkeypatch.setenv(policy.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(policy.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(policy.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(policy.OPERATOR_ALLOWED_PATHS_ENV, str(root))


def test_openhands_is_an_explicit_builtin_backend():
    assert "openhands" in {backend["name"] for backend in runners.list_backends()}
    contract = {"execution": {"backend": "openhands"}}
    assert runners.selected_backend(contract) == "openhands"


def test_openhands_dispatch_is_gated_and_has_no_backend_fallback(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path)
    monkeypatch.setenv(runners.RUNNER_BACKEND_ALLOWLIST_ENV, "openhands")
    backend = OpenHandsBackend(credential_provider=lambda: "")
    contract = _contract(tmp_path)

    preview = backend.dispatch(contract, confirm=False, dry_run=True, timeout=30, hermes_root=tmp_path / "hermes")
    assert preview["success"] and preview["dry_run"] and preview["backend"] == "openhands"
    assert preview["plan"]["timeout_seconds"] == 24 * 60 * 60
    refused = backend.dispatch(contract, confirm=False, dry_run=False, timeout=30, hermes_root=tmp_path / "hermes")
    assert refused["code"] == "CONFIRMATION_REQUIRED"
    missing_auth = backend.dispatch(contract, confirm=True, dry_run=False, timeout=30, hermes_root=tmp_path / "hermes")
    assert missing_auth["code"] == "OPENHANDS_AUTH_REQUIRED"

    monkeypatch.setattr(runners, "get_backend", lambda name: backend if name == "openhands" else (_ for _ in ()).throw(AssertionError("fallback attempted")))
    selected = contracts._parse_contract(json.dumps(contract))[1]
    result = runners.dispatch_contract(selected, confirm=True, dry_run=False, timeout=30, hermes_root=tmp_path / "hermes")
    assert result["code"] == "OPENHANDS_AUTH_REQUIRED"
    assert result["backend"] == "openhands"


def test_openhands_restart_observation_fails_closed_without_persisting_credentials_or_prompt(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path)
    backend = runners.get_backend("openhands")
    runtime = FakeRuntime()
    backend.runtime = runtime
    backend.credential_provider = lambda: "mock-api-key-never-persist"
    contract = _contract(tmp_path)
    root = tmp_path / "hermes"

    dispatched = backend.dispatch(contract, confirm=True, dry_run=False, timeout=30, hermes_root=root)
    assert dispatched["success"] and dispatched["state"] == "running"
    meta_path = root / "runner-jobs" / "openhands-task-001.json"
    stored = meta_path.read_text(encoding="utf-8")
    assert "mock-api-key-never-persist" not in stored
    assert contract["objective"] not in stored
    assert "prompt" not in stored.lower()
    assert runtime.started[0]["api_key"] == "mock-api-key-never-persist"

    runtime.task = {"status": "READY", "app_conversation_id": "conversation-1"}
    assert backend.observed_runs("openhands-task-001", hermes_root=root)[0]["status"] == "running"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["next_poll_at"] = 0
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    runtime.conversation_result = {"execution_status": "unrecognized-result"}
    run = backend.observed_runs("openhands-task-001", hermes_root=root)[0]
    assert run["status"] == "needs_attention"
    assert run["error"] == "OPENHANDS_UNSUPPORTED_STATUS"
    parsed = contracts._parse_contract(json.dumps(contract))[1]
    assert contracts._check_run_state(parsed, root)["status"] == "FAIL"


def test_openhands_rejects_secret_like_prompt_text(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path)
    contract = _contract(tmp_path)
    contract["objective"] = "Use Bearer abcdefghijklmnopqrstuvwxyz123456"
    backend = OpenHandsBackend(credential_provider=lambda: "injected", runtime=FakeRuntime())
    result = backend.dispatch(contract, confirm=True, dry_run=False, timeout=30, hermes_root=tmp_path / "hermes")
    assert result["success"] is False
    assert result["code"] == "OPENHANDS_PROMPT_SECRET_LIKE_CONTENT"
    assert not (tmp_path / "hermes" / "runner-jobs" / "openhands-task-001.json").exists()


def test_openhands_timeout_is_persisted_as_needs_attention(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path)
    runtime = FakeRuntime()
    runtime.start = lambda **_: (_ for _ in ()).throw(TimeoutError())
    backend = OpenHandsBackend(credential_provider=lambda: "injected", runtime=runtime)
    contract = _contract(tmp_path)
    root = tmp_path / "hermes"
    result = backend.dispatch(contract, confirm=True, dry_run=False, timeout=30, hermes_root=root)
    assert result["success"] is False
    assert result["code"] == "OPENHANDS_TIMEOUT"
    assert result["state"] == "needs_attention"
    assert backend.observed_runs("openhands-task-001", hermes_root=root)[0]["status"] == "needs_attention"


@pytest.mark.parametrize(
    ("start_result", "task_result", "conversation_result"),
    [
        ({"app_conversation_id": "conversation-1", "execution_status": "AWAITING_USER_INPUT"}, None, None),
        ({"id": "start-1", "status": "AWAITING_USER_INPUT"}, None, None),
        ({"id": "start-1", "status": "PENDING"}, {"status": "AWAITING_USER_INPUT"}, None),
        ({"id": "start-1", "status": "PENDING"}, {"status": "READY", "app_conversation_id": "conversation-1"}, {"execution_status": "AWAITING_USER_INPUT"}),
    ],
)
def test_openhands_user_wait_states_need_attention(
    tmp_path, monkeypatch, start_result, task_result, conversation_result
):
    _enable(monkeypatch, tmp_path)
    runtime = FakeRuntime()
    runtime.start = lambda **_: start_result
    if task_result is not None:
        runtime.task = task_result
    if conversation_result is not None:
        runtime.conversation_result = conversation_result
    backend = OpenHandsBackend(credential_provider=lambda: "injected", runtime=runtime)
    contract = _contract(tmp_path)
    root = tmp_path / "hermes"

    dispatched = backend.dispatch(contract, confirm=True, dry_run=False, timeout=30, hermes_root=root)
    assert dispatched["state"] in {"running", "needs_attention"}
    observed = backend.observed_runs(contract["task_id"], hermes_root=root)[0]
    assert observed["status"] == "needs_attention"
    assert observed["error"] == "OPENHANDS_USER_INPUT_REQUIRED"
    assert contracts._check_run_state(contracts._parse_contract(json.dumps(contract))[1], root)["status"] == "FAIL"


def test_openhands_contract_deadline_persists_and_expires_after_restart(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path)
    runtime = FakeRuntime()
    backend = OpenHandsBackend(credential_provider=lambda: "injected", runtime=runtime)
    contract = _contract(tmp_path)
    contract["execution"]["options"]["timeout_seconds"] = 60
    root = tmp_path / "hermes"

    dispatched = backend.dispatch(contract, confirm=True, dry_run=False, timeout=30, hermes_root=root)
    assert dispatched["success"] and dispatched["state"] == "running"
    meta_path = root / "runner-jobs" / f"{contract['task_id']}.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    deadline = meta["deadline_at_epoch"]
    cancellation = backend.cancel(contract["task_id"], hermes_root=root)
    assert cancellation["code"] == "RUNNER_CANCEL_UNSUPPORTED"
    assert json.loads(meta_path.read_text(encoding="utf-8"))["state"] == "running"
    assert meta["timeout_seconds"] == 60
    assert 59 <= deadline - time.time() <= 61

    runtime.start_task = lambda **_: pytest.fail("expired OpenHands work must not be polled")
    restarted_backend = OpenHandsBackend(credential_provider=lambda: "injected", runtime=runtime)
    monkeypatch.setattr("operator_openhands.time.time", lambda: deadline + 1)
    observed = restarted_backend.observed_runs(contract["task_id"], hermes_root=root)[0]
    assert observed["status"] == "needs_attention"
    assert observed["outcome"] == "needs_attention"
    assert observed["error"] == "OPENHANDS_CONTRACT_DEADLINE_EXCEEDED"
    assert json.loads(meta_path.read_text(encoding="utf-8"))["deadline_at_epoch"] == deadline
    assert contracts._check_run_state(contracts._parse_contract(json.dumps(contract))[1], root)["status"] == "FAIL"


def test_openhands_active_metadata_without_deadline_fails_closed_before_poll(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path)
    runtime = FakeRuntime()
    backend = OpenHandsBackend(credential_provider=lambda: "injected", runtime=runtime)
    contract = _contract(tmp_path)
    root = tmp_path / "hermes"
    backend.dispatch(contract, confirm=True, dry_run=False, timeout=30, hermes_root=root)

    meta_path = root / "runner-jobs" / f"{contract['task_id']}.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for field in ("timeout_seconds", "started_at_epoch", "deadline_at_epoch"):
        meta.pop(field)
    meta["next_poll_at"] = time.time() + 3600
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    runtime.start_task = lambda **_: pytest.fail("metadata without a deadline must not be polled")

    observed = backend.observed_runs(contract["task_id"], hermes_root=root)[0]
    assert observed["status"] == "needs_attention"
    assert observed["error"] == "OPENHANDS_DEADLINE_MISSING"
    assert json.loads(meta_path.read_text(encoding="utf-8"))["state"] == "needs_attention"
    assert contracts._check_run_state(contracts._parse_contract(json.dumps(contract))[1], root)["status"] == "FAIL"


@pytest.mark.parametrize("timeout_seconds", [True, False, 0, 59, 604801, 60.0, "60"])
def test_openhands_contract_timeout_must_be_bounded_integer(tmp_path, monkeypatch, timeout_seconds):
    _enable(monkeypatch, tmp_path)
    runtime = FakeRuntime()
    backend = OpenHandsBackend(credential_provider=lambda: "injected", runtime=runtime)
    contract = _contract(tmp_path)
    contract["execution"]["options"]["timeout_seconds"] = timeout_seconds
    root = tmp_path / "hermes"

    result = backend.dispatch(contract, confirm=True, dry_run=False, timeout=30, hermes_root=root)
    assert result["success"] is False
    assert result["code"] == "OPENHANDS_TIMEOUT_INVALID"
    assert runtime.started == []
    assert not (root / "runner-jobs" / f"{contract['task_id']}.json").exists()


@pytest.mark.parametrize(
    "error",
    ["OPENHANDS_USER_INPUT_REQUIRED", "OPENHANDS_CONTRACT_DEADLINE_EXCEEDED"],
)
def test_openhands_attention_errors_do_not_auto_retry(error):
    observation = failure_semantics._validate_envelope({
        "delegation": {
            "state": "failed", "backend_state": "needs_attention",
            "outcome": "needs_attention", "validation_verdict": "",
        },
        "runner": {"status": "needs_attention", "outcome": "needs_attention", "error": error},
        "last_failure_error": error,
        "plan": {"node_state": "running", "parent_done": True, "all_children_terminal": False},
        "mission": {"status": "running", "final_approval_required": True},
        "breaker": {"consecutive_failures": 1, "limit": 3, "gave_up": False},
    })

    decision = failure_semantics.classify("mission-1", "node-1", observation)
    assert decision["classification"] == failure_semantics.CLASS_UNKNOWN
    assert decision["auto_retry"] is False
    assert decision["need_attention"] is True


def test_openhands_transient_api_and_parse_errors_are_bounded_failures(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path)
    failures = (
        (urllib.error.URLError("temporarily unavailable"), "OPENHANDS_UNAVAILABLE"),
        (json.JSONDecodeError("invalid response", "{", 0), "OPENHANDS_UNSUPPORTED_RESULT"),
    )
    for failure, code in failures:
        runtime = FakeRuntime()
        runtime.start = lambda error=failure, **_: (_ for _ in ()).throw(error)
        backend = OpenHandsBackend(credential_provider=lambda: "mock-key", runtime=runtime)
        contract = _contract(tmp_path)
        contract["task_id"] = f"openhands-{code.lower()}"
        root = tmp_path / "hermes"

        result = backend.dispatch(
            contract,
            confirm=True,
            dry_run=False,
            timeout=30,
            hermes_root=root,
        )

        assert result["success"] is False
        assert result["code"] == code
        assert result["state"] == "needs_attention"
        observed = backend.observed_runs(contract["task_id"], hermes_root=root)
        assert observed[0]["status"] == "needs_attention"
        meta = (root / "runner-jobs" / f"{contract['task_id']}.json").read_text()
        assert "mock-key" not in meta
        assert "temporarily unavailable" not in meta


def test_openhands_start_and_status_failures_are_observed_not_success(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path)
    backend = OpenHandsBackend(credential_provider=lambda: "injected", runtime=FakeRuntime())
    contract = _contract(tmp_path)
    root = tmp_path / "hermes"
    backend.runtime.start = lambda **_: {"unexpected": "shape"}
    result = backend.dispatch(contract, confirm=True, dry_run=False, timeout=30, hermes_root=root)
    assert result["success"] is False
    assert result["state"] == "needs_attention"
    assert result["code"] == "OPENHANDS_UNSUPPORTED_START_RESPONSE"
    observed = backend.observed_runs("openhands-task-001", hermes_root=root)
    assert observed[0]["status"] == "needs_attention"


def test_http_runtime_uses_injected_bearer_without_persisting_it(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        seen["payload"] = json.loads(request.data)
        seen["timeout"] = timeout
        return io.BytesIO(b'{"id":"start-123","status":"PENDING"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    class FakeOpener:
        def open(self, request, timeout):
            return fake_urlopen(request, timeout)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: FakeOpener())

    response = HttpRuntime().start(
        api_key="only-in-memory-test-credential",
        base_url="https://app.all-hands.dev",
        prompt="bounded work contract",
        repository="owner/repo",
        branch="task-branch",
        timeout=17,
    )
    assert response["id"] == "start-123"
    assert seen["url"] == "https://app.all-hands.dev/api/v1/app-conversations"
    assert seen["authorization"] == "Bearer only-in-memory-test-credential"
    assert seen["payload"]["selected_repository"] == "owner/repo"
    assert seen["payload"]["selected_branch"] == "task-branch"
    assert seen["timeout"] == 17
