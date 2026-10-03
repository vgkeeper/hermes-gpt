"""Explicit OpenHands Cloud runner backend for Work Contracts."""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import operator_job_supervisor as jobs
import operator_policy as op

DEFAULT_BASE_URL = "https://app.all-hands.dev"
BASE_URL_ENV = "HERMES_GPT_OPENHANDS_BASE_URL"
MAX_PROMPT_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_TIMEOUT = 120
DEFAULT_CONTRACT_TIMEOUT_SECONDS = 24 * 60 * 60
MIN_CONTRACT_TIMEOUT_SECONDS = 60
MAX_CONTRACT_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
_USER_WAIT_STATUSES = frozenset({"awaiting_user_input", "waiting_for_user_input", "needs_user_input"})


class UnsupportedResponseError(Exception):
    """The remote runner returned a response outside the supported API shape."""


_RUNTIME_ERRORS = (
    OSError,
    http.client.HTTPException,
    urllib.error.URLError,
    json.JSONDecodeError,
    UnsupportedResponseError,
    TypeError,
    ValueError,
)
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(hermes_root: Path | None) -> Path:
    raw = hermes_root or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    normalized = op.normalize_hermes_data_root(Path(raw).expanduser())
    return Path(normalized or Path.home() / ".hermes") / "runner-jobs"


def _meta_path(task_id: str, hermes_root: Path | None) -> Path:
    return _root(hermes_root) / f"{task_id}.json"


def _read_meta(task_id: str, hermes_root: Path | None) -> dict[str, Any] | None:
    try:
        value = json.loads(_meta_path(task_id, hermes_root).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _write_meta_unlocked(meta: dict[str, Any], hermes_root: Path | None) -> None:
    jobs._atomic_json(_meta_path(str(meta["task_id"]), hermes_root), meta)


def _write_meta(meta: dict[str, Any], hermes_root: Path | None) -> None:
    with jobs._record_lock(str(meta["task_id"]), hermes_root):
        _write_meta_unlocked(meta, hermes_root)


def _base_url() -> str:
    value = os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid_base_url")
    return value


def _credential() -> str:
    return os.environ.get("OPENHANDS_CLOUD_API_KEY", "").strip() or os.environ.get("OPENHANDS_API_KEY", "").strip()


class Runtime(Protocol):
    def start(self, *, api_key: str, base_url: str, prompt: str, repository: str, branch: str, timeout: int) -> dict[str, Any]: ...
    def conversation(self, *, api_key: str, base_url: str, conversation_id: str, timeout: int) -> dict[str, Any]: ...
    def start_task(self, *, api_key: str, base_url: str, task_id: str, timeout: int) -> dict[str, Any]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class HttpRuntime:
    """HTTPS-only OpenHands Cloud V1 API adapter."""

    @staticmethod
    def _request(api_key: str, url: str, *, method: str = "GET", body: dict[str, Any] | None = None, timeout: int = 30) -> Any:
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json", "Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("response_too_large")
        return json.loads(raw)

    def start(self, *, api_key: str, base_url: str, prompt: str, repository: str, branch: str, timeout: int) -> dict[str, Any]:
        result = self._request(
            api_key, f"{base_url}/api/v1/app-conversations", method="POST", timeout=timeout,
            body={"initial_message": {"role": "user", "content": [{"type": "text", "text": prompt}], "run": True},
                  "selected_repository": repository, "selected_branch": branch},
        )
        if not isinstance(result, dict):
            raise UnsupportedResponseError("unsupported_start_response")
        return result

    def _get_one(self, api_key: str, url: str, item_id: str, timeout: int) -> dict[str, Any]:
        result = self._request(api_key, f"{url}?{urllib.parse.urlencode({'ids': item_id})}", timeout=timeout)
        if not isinstance(result, list) or not result or not isinstance(result[0], dict):
            raise UnsupportedResponseError("unsupported_api_response")
        return result[0]

    def conversation(self, *, api_key: str, base_url: str, conversation_id: str, timeout: int) -> dict[str, Any]:
        return self._get_one(api_key, f"{base_url}/api/v1/app-conversations", conversation_id, timeout)

    def start_task(self, *, api_key: str, base_url: str, task_id: str, timeout: int) -> dict[str, Any]:
        return self._get_one(api_key, f"{base_url}/api/v1/app-conversations/start-tasks", task_id, timeout)


def _validate_prompt_content(value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _validate_prompt_content(item)
    elif isinstance(value, list):
        for item in value:
            _validate_prompt_content(item)
    elif isinstance(value, str) and op.redact_output(value) != value:
        raise ValueError("OPENHANDS_PROMPT_SECRET_LIKE_CONTENT")


def _make_prompt(contract: dict[str, Any]) -> str:
    projection = {
        "task_id": contract["task_id"], "objective": contract["objective"],
        "inputs": contract.get("inputs", []), "constraints": contract.get("constraints", []),
        "artifacts": [Path(item["path"]).name for item in contract.get("expected_artifacts", [])],
        "tests": [item.get("name", "") for item in contract.get("tests", [])],
        "completion_criteria": contract.get("completion_criteria", {}),
    }
    _validate_prompt_content(projection)
    prompt = "Implement this coding Work Contract in the selected repository and branch. Follow its constraints and run its required checks. Report only work actually completed. Contract:\n" + json.dumps(projection, ensure_ascii=False)
    if len(prompt.encode()) > MAX_PROMPT_BYTES:
        raise ValueError("OPENHANDS_PROMPT_TOO_LARGE")
    return prompt


def _contract_timeout_seconds(options: dict[str, Any]) -> int:
    value = options.get("timeout_seconds", DEFAULT_CONTRACT_TIMEOUT_SECONDS)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_CONTRACT_TIMEOUT_SECONDS <= value <= MAX_CONTRACT_TIMEOUT_SECONDS
    ):
        raise ValueError("OPENHANDS_TIMEOUT_INVALID")
    return value


def _needs_attention(meta: dict[str, Any], error: str) -> None:
    meta.update(state="needs_attention", outcome="needs_attention", error=error, ended_at=_now())


def _deadline_error(meta: dict[str, Any], now: float | None = None) -> str:
    timeout_seconds = meta.get("timeout_seconds")
    started_at = meta.get("started_at_epoch")
    deadline = meta.get("deadline_at_epoch")
    if timeout_seconds is None or started_at is None or deadline is None:
        return "OPENHANDS_DEADLINE_MISSING"
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not MIN_CONTRACT_TIMEOUT_SECONDS <= timeout_seconds <= MAX_CONTRACT_TIMEOUT_SECONDS
        or isinstance(started_at, bool)
        or not isinstance(started_at, (int, float))
        or isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
    ):
        return "OPENHANDS_DEADLINE_INVALID"
    try:
        started_at = float(started_at)
        deadline = float(deadline)
    except (OverflowError, ValueError):
        return "OPENHANDS_DEADLINE_INVALID"
    if not math.isfinite(started_at) or not math.isfinite(deadline):
        return "OPENHANDS_DEADLINE_INVALID"
    if abs(deadline - started_at - timeout_seconds) > 0.001:
        return "OPENHANDS_DEADLINE_INVALID"
    if (time.time() if now is None else now) >= deadline:
        return "OPENHANDS_CONTRACT_DEADLINE_EXCEEDED"
    return ""


def _request_timeout(meta: dict[str, Any]) -> int:
    remaining = float(meta["deadline_at_epoch"]) - time.time()
    return max(1, min(30, math.ceil(remaining)))


def _state(result: dict[str, Any]) -> tuple[str, str]:
    status = str(result.get("execution_status") or result.get("status") or "").strip().lower().replace("-", "_")
    if status in _USER_WAIT_STATUSES:
        return "needs_attention", "OPENHANDS_USER_INPUT_REQUIRED"
    if status in {"queued", "pending", "created", "starting", "running"}:
        return "running", ""
    if status in {"finished", "completed", "succeeded", "success"}:
        return "completed", ""
    if status in {"failed", "error", "cancelled", "canceled", "stopped"}:
        return "failed", "OPENHANDS_EXECUTION_FAILED"
    return "needs_attention", "OPENHANDS_UNSUPPORTED_STATUS"


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)) or (
        isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (TimeoutError, socket.timeout))
    ):
        return "OPENHANDS_TIMEOUT"
    if isinstance(exc, urllib.error.HTTPError):
        return "OPENHANDS_HTTP_ERROR"
    if isinstance(exc, urllib.error.URLError):
        return "OPENHANDS_UNAVAILABLE"
    if isinstance(exc, (UnsupportedResponseError, ValueError)):
        return "OPENHANDS_UNSUPPORTED_RESULT"
    if isinstance(exc, (OSError, http.client.HTTPException)):
        return "OPENHANDS_UNAVAILABLE"
    return "OPENHANDS_UNAVAILABLE"


class OpenHandsBackend:
    name = "openhands"

    def __init__(self, credential_provider=_credential, runtime: Runtime | None = None):
        self.credential_provider = credential_provider
        self.runtime = runtime or HttpRuntime()

    def availability(self, *, hermes_root: Path | None = None) -> dict[str, Any]:
        try:
            _base_url()
        except ValueError:
            return {"available": False, "reason": "OPENHANDS_CONFIGURATION_INVALID"}
        try:
            available = bool(self.credential_provider())
        except OSError:
            return {"available": False, "reason": "OPENHANDS_AUTH_REQUIRED"}
        return {"available": available, "reason": None if available else "OPENHANDS_AUTH_REQUIRED"}

    def dispatch(self, contract: dict[str, Any], *, confirm: bool, dry_run: bool, timeout: int, hermes_root: Path | None = None, **_: Any) -> dict[str, Any]:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        effective_dry_run = policy.effective_dry_run(dry_run)
        options = (contract.get("execution") or {}).get("options") or {}
        repository, branch = options.get("repository"), options.get("branch")
        task_id = str(contract["task_id"])
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            return {"success": False, "code": "OPENHANDS_REPOSITORY_REQUIRED", "backend": self.name, "task_id": task_id}
        if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch) or ".." in branch.split("/"):
            return {"success": False, "code": "OPENHANDS_BRANCH_REQUIRED", "backend": self.name, "task_id": task_id}
        try:
            contract_timeout = _contract_timeout_seconds(options)
            base_url, prompt = _base_url(), _make_prompt(contract)
        except ValueError as exc:
            return {"success": False, "code": str(exc) if str(exc).startswith("OPENHANDS_") else "OPENHANDS_CONFIGURATION_INVALID", "backend": self.name, "task_id": task_id}
        plan = {
            "backend": self.name, "repository": repository, "branch": branch,
            "task_id": task_id, "timeout_seconds": contract_timeout,
        }
        if effective_dry_run:
            return {"success": True, "dry_run": True, "changed": False, "backend": self.name, "plan": plan}
        if not confirm:
            return {"success": False, "code": "CONFIRMATION_REQUIRED", "backend": self.name, "task_id": task_id}
        try:
            key = self.credential_provider()
        except OSError:
            key = ""
        if not isinstance(key, str) or not key.strip():
            return {"success": False, "code": "OPENHANDS_AUTH_REQUIRED", "backend": self.name, "task_id": task_id}
        started_at = time.time()
        meta = {
            "task_id": task_id, "backend": self.name, "state": "starting", "outcome": "", "error": "",
            "repository": repository, "branch": branch, "timeout_seconds": contract_timeout,
            "started_at_epoch": started_at, "deadline_at_epoch": started_at + contract_timeout,
            "created_at": _now(), "started_at": _now(),
            "ended_at": None, "conversation_id": "", "start_task_id": "", "next_poll_at": 0,
        }
        with jobs._record_lock(task_id, hermes_root):
            if _meta_path(task_id, hermes_root).exists():
                return {"success": False, "code": "RUNNER_JOB_EXISTS", "backend": self.name, "task_id": task_id}
            _write_meta_unlocked(meta, hermes_root)
        try:
            result = self.runtime.start(api_key=key.strip(), base_url=base_url, prompt=prompt, repository=repository, branch=branch, timeout=max(1, min(int(timeout), MAX_TIMEOUT, contract_timeout)))
            if not isinstance(result, dict):
                raise UnsupportedResponseError("unsupported_start_response")
            expired = _deadline_error(meta)
            if expired:
                _needs_attention(meta, expired)
            else:
                conversation_id, start_task_id = result.get("app_conversation_id"), result.get("id")
                if isinstance(conversation_id, str) and _ID.fullmatch(conversation_id):
                    meta["conversation_id"] = conversation_id
                    meta["state"], meta["error"] = _state(result) if result.get("execution_status") or result.get("status") else ("running", "")
                elif isinstance(start_task_id, str) and _ID.fullmatch(start_task_id):
                    start_status = str(result.get("status") or "pending").strip().lower().replace("-", "_")
                    if start_status in _USER_WAIT_STATUSES:
                        meta["start_task_id"] = start_task_id
                        _needs_attention(meta, "OPENHANDS_USER_INPUT_REQUIRED")
                    elif start_status in {"pending", "queued", "starting", "running", "ready"}:
                        meta.update(start_task_id=start_task_id, state="running", outcome="running")
                    else:
                        _needs_attention(meta, "OPENHANDS_UNSUPPORTED_START_RESPONSE")
                else:
                    _needs_attention(meta, "OPENHANDS_UNSUPPORTED_START_RESPONSE")
                meta["outcome"] = meta["state"]
                if meta["state"] in {"failed", "needs_attention"}:
                    meta["ended_at"] = _now()
            _write_meta(meta, hermes_root)
            if meta["state"] in {"failed", "needs_attention"}:
                return {"success": False, "code": meta["error"], "backend": self.name, "task_id": task_id, "state": meta["state"]}
            return {"success": True, "changed": True, "dry_run": False, "backend": self.name, "task_id": task_id, "state": meta["state"]}
        except _RUNTIME_ERRORS as exc:
            _needs_attention(meta, _deadline_error(meta) or _failure_code(exc))
            _write_meta(meta, hermes_root)
            return {"success": False, "code": meta["error"], "backend": self.name, "task_id": task_id, "state": "needs_attention"}
        finally:
            del key

    def _refresh(self, meta: dict[str, Any], hermes_root: Path | None, *, locked: bool = False) -> dict[str, Any]:
        if meta.get("state") in {"completed", "failed", "cancelled", "needs_attention"}:
            return meta
        deadline_error = _deadline_error(meta)
        if deadline_error:
            _needs_attention(meta, deadline_error)
            if locked:
                _write_meta_unlocked(meta, hermes_root)
            else:
                _write_meta(meta, hermes_root)
            return meta
        if time.time() < float(meta.get("next_poll_at") or 0):
            return meta
        try:
            key = self.credential_provider()
        except OSError:
            key = ""
        deadline_error = _deadline_error(meta)
        if deadline_error:
            _needs_attention(meta, deadline_error)
        elif not isinstance(key, str) or not key.strip():
            _needs_attention(meta, "OPENHANDS_AUTH_REQUIRED")
        else:
            try:
                base_url = _base_url()
                waiting_for_start = False
                request_timeout = _request_timeout(meta)
                if meta.get("conversation_id"):
                    result = self.runtime.conversation(
                        api_key=key, base_url=base_url, conversation_id=meta["conversation_id"], timeout=request_timeout,
                    )
                elif meta.get("start_task_id"):
                    task = self.runtime.start_task(
                        api_key=key, base_url=base_url, task_id=meta["start_task_id"], timeout=request_timeout,
                    )
                    if not isinstance(task, dict):
                        raise ValueError("unsupported_start_task_response")
                    start_status = str(task.get("status") or "").strip().lower().replace("-", "_")
                    if start_status in {"error", "failed", "cancelled", "canceled"}:
                        meta.update(state="failed", outcome="failed", error="OPENHANDS_START_FAILED", ended_at=_now())
                        result = None
                        waiting_for_start = True
                    elif start_status in _USER_WAIT_STATUSES:
                        _needs_attention(meta, "OPENHANDS_USER_INPUT_REQUIRED")
                        result = None
                        waiting_for_start = True
                    elif start_status in {"ready", "done", "completed"}:
                        conversation_id = task.get("app_conversation_id")
                        if not isinstance(conversation_id, str) or not _ID.fullmatch(conversation_id):
                            raise ValueError("unsupported_start_task_response")
                        meta["conversation_id"] = conversation_id
                        result = self.runtime.conversation(
                            api_key=key, base_url=base_url, conversation_id=conversation_id,
                            timeout=_request_timeout(meta),
                        )
                    elif start_status in {"pending", "queued", "running", "starting", "processing"}:
                        meta.update(state="running", outcome="running")
                        waiting_for_start = True
                        result = None
                    else:
                        raise ValueError("unsupported_start_task_status")
                else:
                    raise ValueError("unsupported_runner_state")
                if result is None and not waiting_for_start:
                    raise TypeError("unsupported_conversation_response")
                if result is not None:
                    if not isinstance(result, dict):
                        raise TypeError("unsupported_conversation_response")
                    meta["state"], meta["error"] = _state(result)
                    meta["outcome"] = meta["state"]
                    if meta["state"] in {"completed", "failed", "needs_attention"}:
                        meta["ended_at"] = _now()
                expired = _deadline_error(meta)
                if expired:
                    _needs_attention(meta, expired)
                else:
                    meta["next_poll_at"] = time.time() + 5
            except _RUNTIME_ERRORS as exc:
                _needs_attention(meta, _failure_code(exc))
            finally:
                del key
        if locked:
            _write_meta_unlocked(meta, hermes_root)
        else:
            _write_meta(meta, hermes_root)
        return meta

    def observed_runs(self, task_id: str, *, hermes_root: Path | None = None) -> list[dict[str, Any]]:
        if not _meta_path(task_id, hermes_root).is_file():
            return []
        with jobs._record_lock(task_id, hermes_root):
            meta = _read_meta(task_id, hermes_root)
            if not meta or meta.get("backend") != self.name:
                return []
            meta = self._refresh(meta, hermes_root, locked=True)
            return [{"task_id": task_id, "status": meta["state"], "outcome": meta.get("outcome") or meta["state"], "error": meta.get("error") or None, "started_at": meta.get("started_at"), "ended_at": meta.get("ended_at"), "scope": "runner:openhands"}]

    def cancel(self, task_id: str, *, hermes_root: Path | None = None) -> dict[str, Any]:
        meta = _read_meta(task_id, hermes_root)
        if not meta or meta.get("backend") != self.name:
            return {"success": False, "code": "RUNNER_JOB_NOT_FOUND", "backend": self.name}
        return {"success": False, "code": "RUNNER_CANCEL_UNSUPPORTED", "backend": self.name, "task_id": task_id}


__all__ = ["HttpRuntime", "OpenHandsBackend", "Runtime"]
