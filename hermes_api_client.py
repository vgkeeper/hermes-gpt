"""Minimal stdlib client for the official Hermes Agent HTTP API."""
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
from pathlib import Path
from typing import Any

BASE_URL_ENV = "HERMES_API_BASE_URL"
API_KEY_ENV = "API_SERVER_KEY"
HERMES_HOME_ENV = "HERMES_HOME"
DEFAULT_BASE_URL = "http://hermes-agent:8642"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_PROFILE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class HermesAPIError(Exception):
    """Safe API error containing only a category and optional HTTP status."""

    def __init__(self, category: str, status_code: int | None = None):
        self.category = category
        self.status_code = status_code
        message = f"Hermes API error ({category}"
        if status_code is not None:
            message += f", status={status_code}"
        super().__init__(message + ")")


def _read_env_file_key() -> str:
    home = Path(os.environ.get(HERMES_HOME_ENV) or (Path.home() / ".hermes"))
    try:
        lines = (home / ".env").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip() == API_KEY_ENV:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value.strip()
    return ""


class HermesAPIClient:
    """Call selected endpoints of the official Hermes Agent HTTP API."""

    def __init__(self, base_url: str | None = None, *, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        configured_url = base_url if base_url is not None else os.getenv(BASE_URL_ENV, DEFAULT_BASE_URL)
        self._base_url = self._validate_base_url(configured_url)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise HermesAPIError("invalid_configuration")
        self._timeout = float(timeout)
        env_key = os.environ.get(API_KEY_ENV)
        self._api_key = (env_key if env_key is not None else _read_env_file_key()).strip()
        if not self._api_key:
            raise HermesAPIError("api_key_missing")

    def __repr__(self) -> str:
        return f"HermesAPIClient(timeout={self._timeout!r})"

    @staticmethod
    def _validate_base_url(value: str) -> str:
        if not isinstance(value, str):
            raise HermesAPIError("invalid_configuration")
        value = value.strip().rstrip("/")
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError:
            raise HermesAPIError("invalid_configuration") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise HermesAPIError("invalid_configuration")
        return value

    @staticmethod
    def _validate_profile(profile: str) -> str:
        if not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile):
            raise HermesAPIError("invalid_input")
        return profile

    @staticmethod
    def _validate_id(value: str) -> str:
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise HermesAPIError("invalid_input")
        return value

    @classmethod
    def _route(cls, path: str, profile: str) -> str:
        profile = cls._validate_profile(profile)
        return path if profile == "default" else f"/p/{profile}{path}"

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expected_status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            request_headers = {"Accept": "application/json", "Authorization": f"Bearer {self._api_key}"}
            if data is not None:
                request_headers["Content-Type"] = "application/json"
            if headers:
                request_headers.update(headers)
            request = urllib.request.Request(
                f"{self._base_url}{path}", data=data, headers=request_headers, method=method)
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise HermesAPIError("http_error", status) from None
        except (TimeoutError, socket.timeout):
            raise HermesAPIError("timeout") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise HermesAPIError("timeout") from None
            raise HermesAPIError("network_error") from None
        except (http.client.HTTPException, OSError, ValueError):
            raise HermesAPIError("network_error") from None

        if status != expected_status:
            raise HermesAPIError("http_error", status)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise HermesAPIError("invalid_response")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HermesAPIError("invalid_response") from None
        if not isinstance(result, dict):
            raise HermesAPIError("invalid_response")
        return result

    def health(self, profile: str = "default") -> dict[str, Any]:
        """Return the ``GET /health`` response (the endpoint itself is unauthenticated)."""
        return self._request_json("GET", self._route("/health", profile))

    def capabilities(self, profile: str = "default") -> dict[str, Any]:
        """Return ``GET /v1/capabilities``."""
        return self._request_json("GET", self._route("/v1/capabilities", profile))

    def create_session(
        self,
        session_id: str | None = None,
        title: str | None = None,
        source: str | None = None,
        *,
        id: str | None = None,
        profile: str = "default",
    ) -> dict[str, Any]:
        """Create a session using only the minimal supported fields (HTTP 201)."""
        payload: dict[str, Any] = {}
        if id is not None and session_id is not None:
            raise HermesAPIError("invalid_input")
        selected_id = session_id if session_id is not None else id
        if selected_id is not None:
            payload["session_id"] = self._validate_id(selected_id)
        for name, value in (("title", title), ("source", source)):
            if value is not None:
                if not isinstance(value, str):
                    raise HermesAPIError("invalid_input")
                payload[name] = value
        return self._request_json(
            "POST", self._route("/api/sessions", profile), payload=payload, expected_status=201
        )

    def create_run(
        self,
        input: str,
        session_id: str | None = None,
        profile: str = "default",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Submit user input to ``POST /v1/runs`` (HTTP 202)."""
        if not isinstance(input, str) or not input.strip():
            raise HermesAPIError("invalid_input")
        payload: dict[str, Any] = {"input": input}
        if session_id is not None:
            payload["session_id"] = self._validate_id(session_id)
        extra_headers: dict[str, str] = {}
        if idempotency_key is not None:
            if (
                not isinstance(idempotency_key, str)
                or not 1 <= len(idempotency_key) <= 255
                or any(ord(char) < 0x21 or ord(char) > 0x7E for char in idempotency_key)
            ):
                raise HermesAPIError("invalid_input")
            extra_headers["Idempotency-Key"] = idempotency_key
        return self._request_json(
            "POST", self._route("/v1/runs", profile), payload=payload,
            expected_status=202, headers=extra_headers
        )

    def get_run(self, run_id: str, profile: str = "default") -> dict[str, Any]:
        """Return the pollable status from ``GET /v1/runs/{run_id}``."""
        run_id = self._validate_id(run_id)
        path_id = urllib.parse.quote(run_id, safe="")
        return self._request_json("GET", self._route(f"/v1/runs/{path_id}", profile))

    def stop_run(self, run_id: str, profile: str = "default") -> dict[str, Any]:
        """Stop a run using ``POST /v1/runs/{run_id}/stop`` (HTTP 200)."""
        run_id = self._validate_id(run_id)
        path_id = urllib.parse.quote(run_id, safe="")
        return self._request_json(
            "POST", self._route(f"/v1/runs/{path_id}/stop", profile), expected_status=200
        )

    def iter_run_events(
        self, run_id: str, profile: str = "default", *,
        stream_deadline_monotonic: float | None = None,
    ):
        """Yield decoded SSE events, optionally ending at a monotonic deadline.

        Each yielded value is the JSON object payload. When an SSE ``event``
        name is present, it is included as the payload's ``event`` field.
        The deadline is checked for every line, including keepalive comments.
        """
        if stream_deadline_monotonic is not None and (
            isinstance(stream_deadline_monotonic, bool)
            or not isinstance(stream_deadline_monotonic, (int, float))
            or not math.isfinite(stream_deadline_monotonic)
        ):
            raise HermesAPIError("invalid_input")
        run_id = self._validate_id(run_id)
        path_id = urllib.parse.quote(run_id, safe="")
        path = self._route(f"/v1/runs/{path_id}/events", profile)
        try:
            request = urllib.request.Request(
                f"{self._base_url}{path}",
                headers={
                    "Accept": "text/event-stream",
                    "Authorization": f"Bearer {self._api_key}",
                },
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=15.0) as response:
                if response.status != 200:
                    raise HermesAPIError("http_error", response.status)
                event_name: str | None = None
                data_lines: list[str] = []
                frame_bytes = 0
                while True:
                    if stream_deadline_monotonic is not None:
                        remaining = stream_deadline_monotonic - time.monotonic()
                        if remaining <= 0:
                            raise HermesAPIError("stream_deadline")
                        try:
                            sock = response.fp.raw._sock
                            sock.settimeout(min(max(remaining, 0.001), 15.0))
                        except (AttributeError, OSError):
                            pass
                    raw_line = response.readline(MAX_RESPONSE_BYTES + 1)
                    if stream_deadline_monotonic is not None and time.monotonic() >= stream_deadline_monotonic:
                        raise HermesAPIError("stream_deadline")
                    if not raw_line:
                        break
                    frame_bytes += len(raw_line)
                    if len(raw_line) > MAX_RESPONSE_BYTES or frame_bytes > MAX_RESPONSE_BYTES:
                        raise HermesAPIError("invalid_response")
                    try:
                        line = raw_line.decode("utf-8").rstrip("\r\n")
                    except UnicodeDecodeError:
                        raise HermesAPIError("invalid_response") from None
                    if not line:
                        if data_lines:
                            try:
                                payload = json.loads("\n".join(data_lines))
                            except json.JSONDecodeError:
                                raise HermesAPIError("invalid_response") from None
                            if not isinstance(payload, dict):
                                raise HermesAPIError("invalid_response")
                            if event_name is not None:
                                payload = {**payload, "event": event_name}
                            yield payload
                        event_name = None
                        data_lines = []
                        frame_bytes = 0
                        continue
                    if line.startswith(":"):
                        continue
                    field, separator, value = line.partition(":")
                    if not separator:
                        continue
                    if value.startswith(" "):
                        value = value[1:]
                    if field == "event":
                        event_name = value
                    elif field == "data":
                        data_lines.append(value)
                if data_lines:
                    try:
                        payload = json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        raise HermesAPIError("invalid_response") from None
                    if not isinstance(payload, dict):
                        raise HermesAPIError("invalid_response")
                    if event_name is not None:
                        payload = {**payload, "event": event_name}
                    yield payload
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise HermesAPIError("http_error", status) from None
        except (TimeoutError, socket.timeout):
            raise HermesAPIError("timeout") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise HermesAPIError("timeout") from None
            raise HermesAPIError("network_error") from None
        except HermesAPIError:
            raise
        except (http.client.HTTPException, OSError, ValueError):
            raise HermesAPIError("network_error") from None

    def get_session(self, session_id: str, profile: str = "default") -> dict[str, Any]:
        """Return a session from ``GET /api/sessions/{session_id}``."""
        session_id = self._validate_id(session_id)
        path_id = urllib.parse.quote(session_id, safe="")
        return self._request_json("GET", self._route(f"/api/sessions/{path_id}", profile))
