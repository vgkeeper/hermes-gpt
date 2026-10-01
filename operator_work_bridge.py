"""Narrow Hermes Work adapter for the independent Hermes Work Bridge service."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

URL_ENV = "HERMES_WORK_BRIDGE_URL"
TOKEN_ENV = "HERMES_WORK_BRIDGE_TOKEN"
TOKEN_FILE_ENV = "HERMES_WORK_BRIDGE_TOKEN_FILE"
TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 64 * 1024


def _token() -> str:
    token = os.getenv(TOKEN_ENV, "").strip()
    if token:
        return token
    path = os.getenv(TOKEN_FILE_ENV, "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _base_url() -> str:
    value = os.getenv(URL_ENV, "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower()
    private_http = (
        parsed.scheme == "http"
        and (host in {"localhost", "hermes-work-bridge"} or host.endswith(".internal"))
    )
    if parsed.scheme != "https" and not private_http:
        raise ValueError("Bridge URL must use HTTPS or a configured private service hostname.")
    if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Bridge URL is invalid.")
    return value


def _call(method: str, path: str, payload: dict[str, Any] | None = None) -> str:
    token = _token()
    if not token:
        return json.dumps({"success": False, "code": "BRIDGE_NOT_CONFIGURED"})
    try:
        url = _base_url() + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return json.dumps({"success": False, "code": "BRIDGE_RESPONSE_TOO_LARGE"})
            result = json.loads(raw)
        return json.dumps({"success": True, "result": result}, ensure_ascii=False)
    except urllib.error.HTTPError as exc:
        return json.dumps({"success": False, "code": "BRIDGE_HTTP_ERROR", "http_status": exc.code})
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return json.dumps({"success": False, "code": "BRIDGE_UNAVAILABLE", "detail": type(exc).__name__})


def hermes_work_mission_register(project_id: str, mission_id: str, session_id: str, job_id: str) -> str:
    """Register one Work mission and its dedicated Hermes session/current job."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", project_id):
        return json.dumps({"success": False, "code": "INVALID_PROJECT_ID"})
    if not re.fullmatch(re.escape(project_id) + r"_m[0-9]{3,}", mission_id):
        return json.dumps({"success": False, "code": "INVALID_MISSION_ID"})
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        return json.dumps({"success": False, "code": "INVALID_SESSION_OR_JOB_ID"})
    return _call("POST", "/v1/missions", {"project_id": project_id, "mission_id": mission_id, "session_id": session_id, "job_id": job_id})


def hermes_work_mission_update_job(mission_id: str, job_id: str) -> str:
    """Replace the watched job after Work starts the next Hermes continuation."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}_m[0-9]{3,}", mission_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        return json.dumps({"success": False, "code": "INVALID_MISSION_OR_JOB_ID"})
    return _call("PUT", f"/v1/missions/{mission_id}/job", {"job_id": job_id})


def hermes_work_mission_get(mission_id: str) -> str:
    """Read the persisted Work-to-Hermes mission/session/job association."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}_m[0-9]{3,}", mission_id):
        return json.dumps({"success": False, "code": "INVALID_MISSION_ID"})
    return _call("GET", f"/v1/missions/{mission_id}")


def hermes_work_mission_cancel(mission_id: str) -> str:
    """Disable notifications for a cancelled/closed Work mission."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}_m[0-9]{3,}", mission_id):
        return json.dumps({"success": False, "code": "INVALID_MISSION_ID"})
    return _call("POST", f"/v1/missions/{mission_id}/cancel")


def register_tools(server: Any, *, meta: dict[str, Any] | None = None) -> None:
    """Register only the bridge adapter operations on the existing Pilote MCP."""
    for tool in (
        hermes_work_mission_register,
        hermes_work_mission_update_job,
        hermes_work_mission_get,
        hermes_work_mission_cancel,
    ):
        server.add_tool(tool, meta=meta)
