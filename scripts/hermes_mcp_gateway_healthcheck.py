"""Container health probe for the dedicated Hermes MCP gateway."""
from __future__ import annotations

import os
import urllib.error
import urllib.request

SERVER_HEALTH_URL = "http://127.0.0.1:17678/"
TUNNEL_READY_URL = "http://127.0.0.1:17679/readyz"
TUNNEL_ENABLED_ENV = "HERMES_MCP_GATEWAY_TUNNEL_ENABLED"


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def main() -> int:
    if not _healthy(SERVER_HEALTH_URL):
        return 1
    if os.getenv(TUNNEL_ENABLED_ENV, "0").strip() == "1" and not _healthy(TUNNEL_READY_URL):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
