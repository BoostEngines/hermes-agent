"""A private-chat slot survives shared load without changing action authority."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from gateway.config import GatewayConfig, Platform, load_gateway_config
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli.active_sessions import (
    active_session_registry_snapshot,
    resolve_reserved_dm_session,
    try_acquire_active_session,
)

OWNER = "ou_test_owner"
CONFIG = {"max_concurrent_sessions": 16,
          "reserved_dm_session": {"platform": "feishu", "user_id": OWNER}}


def runner(config):
    value = object.__new__(GatewayRunner)
    value.config = config
    value._running_agents = {}
    return value


def owner_source(**overrides):
    return SessionSource(**{
        "platform": Platform.FEISHU, "chat_id": "oc_private", "chat_type": "dm",
        "user_id": "tenant_user", "user_id_open": OWNER, **overrides,
    })


def test_yaml_to_gateway_reserves_one_slot_for_verified_dm(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(CONFIG))
    config = load_gateway_config()
    assert config.reserved_dm_session == CONFIG["reserved_dm_session"]
    assert GatewayConfig.from_dict(config.to_dict()).reserved_dm_session == config.reserved_dm_session
    gateway = runner(config)
    held = []
    try:
        for n in range(15):
            source = SessionSource(platform=Platform.WEBHOOK, chat_id=f"incident-{n}")
            key = build_session_key(source)
            lease, error = gateway._claim_active_session_slot(key, source)
            assert lease is not None and error is None
            held.append(lease)
            gateway._running_agents[key] = object()
        # Same name/text cannot impersonate the event identity; groups and bots
        # also cannot use the private-chat reservation.
        for source in (owner_source(user_id_open="ou_other", user_name=OWNER),
                       owner_source(chat_type="group"), owner_source(is_bot=True),
                       owner_source(platform=Platform.WEBHOOK)):
            source = SessionSource.from_dict(source.to_dict())
            lease, error = gateway._claim_active_session_slot("rejected", source)
            assert lease is None and "shared sessions are full" in error
        source = owner_source()
        restored = SessionSource.from_dict(source.to_dict())
        assert build_session_key(source) == build_session_key(restored)
        lease, error = gateway._claim_active_session_slot(build_session_key(restored), restored)
        assert lease is not None and error is None
        held.append(lease)
        gateway._running_agents[build_session_key(restored)] = object()
        entries = active_session_registry_snapshot()
        assert len(entries) == 16
        assert sum(e.get("reserved_dm") is True for e in entries) == 1
        # An already-running DM retains its existing turn/queue slot.
        assert gateway._claim_active_session_slot(build_session_key(restored), restored) == (None, None)
        lease2, error = gateway._claim_active_session_slot("another-owner-session", restored)
        assert lease2 is None and "active session limit" in error
        # Releasing a shared slot allows another shared session, even while the
        # reserved DM is still active (15 shared + 1 reserved, not 14 + 1).
        held.pop(0).release()
        lease2, error = try_acquire_active_session(session_id="replacement", surface="cli", config=CONFIG)
        assert lease2 is not None and error is None
        held.append(lease2)
    finally:
        for lease in held:
            lease.release()


def test_parallel_processes_cannot_borrow_the_reserved_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    held = [try_acquire_active_session(session_id=str(n), surface="cli", config=CONFIG)[0]
            for n in range(14)]
    children = []
    try:
        code = f"""
import sys
from hermes_cli.active_sessions import try_acquire_active_session
lease, error = try_acquire_active_session(session_id=sys.argv[1], surface='cli', config={CONFIG!r})
print('OK' if lease else 'BLOCK', flush=True)
sys.stdin.readline()
if lease: lease.release()
"""
        for n in range(4):
            children.append(subprocess.Popen(
                [sys.executable, "-c", code, str(n)],
                cwd=Path(__file__).resolve().parents[2], env=os.environ.copy(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        results = [child.stdout.readline().strip() for child in children]
        assert results.count("OK") == 1 and results.count("BLOCK") == 3
        gateway = runner(GatewayConfig.from_dict(CONFIG))
        lease, error = gateway._claim_active_session_slot("owner", owner_source())
        assert lease is not None and error is None
        held.append(lease)
        assert len(active_session_registry_snapshot()) == 16
    finally:
        for child in children:
            try:
                child.communicate("done\n", timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
        for lease in held:
            if lease:
                lease.release()


def test_reservation_fails_closed_on_registry_or_config_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for broken in ("invalid json", json.dumps({"entries": "bad"}), json.dumps({"entries": [None]})):
        (runtime / "active_sessions.json").write_text(broken)
        lease, error = runner(GatewayConfig.from_dict(CONFIG))._claim_active_session_slot("owner", owner_source())
        assert lease is None and "temporarily unavailable" in error
        lease, error = try_acquire_active_session(session_id="cli", surface="cli", config=CONFIG)
        assert lease is None and "temporarily unavailable" in error
    for config in ({**CONFIG, "max_concurrent_sessions": None},
                   {**CONFIG, "reserved_dm_session": {"platform": "feishu", "user_id": ""}}):
        with pytest.raises(ValueError, match="reserved_dm_session"):
            resolve_reserved_dm_session(config)
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
        with pytest.raises(ValueError, match="reserved_dm_session"):
            load_gateway_config()
        lease, error = try_acquire_active_session(session_id="cli", surface="cli", config=config)
        assert lease is None and "temporarily unavailable" in error

    from hermes_cli.active_sessions import _FileLock

    def inaccessible(_self):
        raise PermissionError("test registry lock unavailable")

    monkeypatch.setattr(_FileLock, "__enter__", inaccessible)
    lease, error = try_acquire_active_session(session_id="cli", surface="cli", config=CONFIG)
    assert lease is None and "temporarily unavailable" in error
