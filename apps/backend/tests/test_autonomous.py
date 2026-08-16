from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY

import pytest

from app.autonomous import AutonomousGovernanceClient, AutonomousProvisionError


class FakeCursor:
    def __init__(self, calls: list[tuple[str, list[str]]]) -> None:
        self.calls = calls

    def callproc(self, name: str, arguments: list[str]) -> None:
        self.calls.append((name, arguments))


class FakeConnection:
    def __init__(self, calls: list[tuple[str, list[str]]]) -> None:
        self.calls = calls
        self.commits = 0

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.calls)

    def commit(self) -> None:
        self.commits += 1


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    wallet = tmp_path / "autonomous"
    wallet.mkdir()
    (wallet / "wallet.zip").write_bytes(b"canonical-wallet")
    runtime = {
        "schema_version": 1,
        "bootstrap_version": 2,
        "operator_username": "AIDP_LAB_OPERATOR",
        "operator_password": "OperatorPassword1234567890",
        "wallet_password": "WalletPassword123",
        "dsn": "aidp_low",
    }
    runtime_file = wallet / "runtime.json"
    runtime_file.write_text(json.dumps(runtime), encoding="utf-8")
    calls: list[tuple[str, list[str]]] = []
    connections: list[dict[str, str]] = []

    def connect(**kwargs: str) -> FakeConnection:
        connections.append(kwargs)
        return FakeConnection(calls)

    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(connect=connect))
    return (
        AutonomousGovernanceClient(
            str(runtime_file), str(tmp_path / "state" / "autonomous-users.json")
        ),
        calls,
        connections,
    )


def test_participant_autonomous_lifecycle_is_exact_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, calls, connections = _client(tmp_path, monkeypatch)

    first = client.ensure_participant("u101")
    second = client.ensure_participant("u101")
    client.ensure_participant("u102")
    client.drop_participant("u101")

    assert first.owner == "U101_AGENT"
    assert first.reader == "U101_AGENT_RO"
    assert first.reader_password == second.reader_password
    assert first.wallet_zip == b"canonical-wallet"
    assert calls == [
        (
            "ADMIN.AIDP_LAB_GOVERNANCE.ENSURE_PARTICIPANT",
            ["u101", ANY, first.reader_password],
        ),
        (
            "ADMIN.AIDP_LAB_GOVERNANCE.ENSURE_PARTICIPANT",
            ["u101", ANY, first.reader_password],
        ),
        (
            "ADMIN.AIDP_LAB_GOVERNANCE.ENSURE_PARTICIPANT",
            ["u102", ANY, ANY],
        ),
        ("ADMIN.AIDP_LAB_GOVERNANCE.DROP_PARTICIPANT", ["u101"]),
    ]
    state = json.loads(client.state_file.read_text(encoding="utf-8"))
    assert set(state) == {"u102"}
    assert all(item["user"] == "AIDP_LAB_OPERATOR" for item in connections)


def test_autonomous_runtime_fails_closed_on_unknown_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _calls, _connections = _client(tmp_path, monkeypatch)
    runtime = json.loads(client.runtime_file.read_text(encoding="utf-8"))
    runtime["admin_password"] = "must-never-be-accepted"
    client.runtime_file.write_text(json.dumps(runtime), encoding="utf-8")

    assert client.ready() is False
    with pytest.raises(AutonomousProvisionError, match="invalid"):
        client.ensure_participant("u101")


def test_missing_wallet_fails_before_creating_database_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, calls, _connections = _client(tmp_path, monkeypatch)
    (client.runtime_file.parent / "wallet.zip").unlink()

    assert client.ready() is False
    with pytest.raises(AutonomousProvisionError, match="wallet is unavailable"):
        client.ensure_participant("u101")
    assert calls == []


def test_database_failures_report_only_safe_error_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _calls, _connections = _client(tmp_path, monkeypatch)

    def connect(**_kwargs: str) -> FakeConnection:
        raise RuntimeError("ORA-01017: invalid credential password=must-not-leak")

    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(connect=connect))

    with pytest.raises(AutonomousProvisionError) as raised:
        client.ensure_participant("u101")
    assert str(raised.value).endswith("(ORA-01017)")
    assert "must-not-leak" not in str(raised.value)
