"""Hermeticity regression tests (audit t_9d200636, Class A).

The suite historically read the developer's real ``~/.hermes/config.yaml``
during fleet/swarm/contract tests (via ``operator_fleet._load_hermes_config``
-> ``hermes_cli`` config resolution). These tests pin the isolation property:

1. under the default test environment the fleet registry must see NO peers
   (the hermetic sandbox has no ``a2a_agents`` config);
"""

from __future__ import annotations

import os
from pathlib import Path

import operator_fleet


def test_fleet_registry_reads_no_real_machine_peers_under_default_test_env():
    """No test may observe the invoking machine's real a2a_agents config."""
    peers = operator_fleet._a2a_peers()
    assert peers == {}, (
        "operator_fleet read a real config.yaml during tests "
        f"(HERMES_HOME={os.environ.get('HERMES_HOME')!r}, peers={sorted(peers)})"
    )


def test_hermes_home_is_redirected_to_a_sandbox_without_real_config():
    home = os.environ.get("HERMES_HOME")
    assert home, "conftest must redirect HERMES_HOME for the whole suite"
    sandbox = Path(home)
    assert sandbox != Path.home() / ".hermes", (
        "HERMES_HOME still points at the invoking user's real data root"
    )
    config = sandbox / "config.yaml"
    if config.exists():
        raw = config.read_text(encoding="utf-8", errors="replace")
        assert "a2a_agents" not in raw, "sandbox config leaks real A2A peers"
    profile = os.environ.get("HERMES_PROFILE")
    assert profile in (None, ""), f"HERMES_PROFILE leaks the invoking shell: {profile!r}"


def test_token_keys_stay_in_the_sandbox(tmp_path):
    """Default token-store persistence cannot reach an OS keychain."""
    import token_store

    try:
        import keyring
        from keyring.backends.fail import Keyring
    except ImportError:
        pass
    else:
        # Fail before resolving a key if a plugin replaced test isolation.
        assert isinstance(keyring.get_keyring(), Keyring)
    key, _kid, source = token_store._resolve_key(tmp_path)
    assert source == "keyfile"
    assert len(key) == 32
    assert token_store.key_file_path(tmp_path).is_file()
