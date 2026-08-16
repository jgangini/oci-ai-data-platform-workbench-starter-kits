"""Least-privilege Autonomous lifecycle for participant governance schemas."""

from __future__ import annotations

import json
import os
import re
import secrets
import string
import threading
from dataclasses import dataclass
from pathlib import Path

from .governance import database_names


class AutonomousProvisionError(RuntimeError):
    pass


def _database_error_marker(exc: Exception) -> str:
    """Return a diagnostic category without leaking connection or credential details."""
    match = re.search(r"\b(?:ORA|DPY)-\d+\b", str(exc), re.IGNORECASE)
    return match.group(0).upper() if match else type(exc).__name__


@dataclass(frozen=True, slots=True)
class ParticipantDatabase:
    owner: str
    reader: str
    reader_password: str
    dsn: str
    wallet_password: str
    wallet_zip: bytes


class AutonomousGovernanceClient:
    """Calls only the ADMIN-owned allowlisted package through AIDP_LAB_OPERATOR."""

    def __init__(self, runtime_file: str, state_file: str = "/var/lib/aidp-lab/autonomous-users.json") -> None:
        self.runtime_file = Path(runtime_file)
        self.state_file = Path(state_file)
        self._lock = threading.Lock()

    def ready(self) -> bool:
        try:
            runtime = self._runtime()
            self._wallet()
            return bool(runtime["operator_username"])
        except (AutonomousProvisionError, OSError):
            return False

    def ensure_participant(self, participant_key: str) -> ParticipantDatabase:
        owner, reader = database_names(participant_key)
        with self._lock:
            wallet = self._wallet()
            users = self._users()
            credentials = users.get(participant_key)
            if not isinstance(credentials, dict):
                credentials = {
                    "owner_password": _password(),
                    "reader_password": _password(),
                }
            runtime = self._runtime()
            try:
                with self._connect(runtime) as connection:
                    cursor = connection.cursor()
                    cursor.callproc(
                        "ADMIN.AIDP_LAB_GOVERNANCE.ENSURE_PARTICIPANT",
                        [
                            participant_key,
                            credentials["owner_password"],
                            credentials["reader_password"],
                        ],
                    )
                    connection.commit()
            except Exception as exc:
                raise AutonomousProvisionError(
                    "Autonomous rejected the participant governance schema reconciliation "
                    f"({_database_error_marker(exc)})"
                ) from exc
            users[participant_key] = credentials
            self._write_users(users)
            return ParticipantDatabase(
                owner=owner,
                reader=reader,
                reader_password=str(credentials["reader_password"]),
                dsn=str(runtime["dsn"]),
                wallet_password=str(runtime["wallet_password"]),
                wallet_zip=wallet,
            )

    def drop_participant(self, participant_key: str) -> None:
        database_names(participant_key)
        with self._lock:
            runtime = self._runtime()
            try:
                with self._connect(runtime) as connection:
                    cursor = connection.cursor()
                    cursor.callproc(
                        "ADMIN.AIDP_LAB_GOVERNANCE.DROP_PARTICIPANT",
                        [participant_key],
                    )
                    connection.commit()
            except Exception as exc:
                raise AutonomousProvisionError(
                    "Autonomous rejected the participant governance schema cleanup "
                    f"({_database_error_marker(exc)})"
                ) from exc
            users = self._users()
            users.pop(participant_key, None)
            self._write_users(users)

    def _runtime(self) -> dict[str, str | int]:
        try:
            value = json.loads(self.runtime_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise AutonomousProvisionError("Autonomous runtime credentials are unavailable") from exc
        required = {
            "schema_version",
            "bootstrap_version",
            "operator_username",
            "operator_password",
            "wallet_password",
            "dsn",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise AutonomousProvisionError("Autonomous runtime credentials are invalid")
        if value.get("schema_version") != 1 or value.get("bootstrap_version") != 2:
            raise AutonomousProvisionError("Autonomous runtime credentials require bootstrap v2")
        if any(not isinstance(value[name], str) or not value[name] for name in required - {"schema_version", "bootstrap_version"}):
            raise AutonomousProvisionError("Autonomous runtime credentials are incomplete")
        return value

    def _wallet_dir(self) -> Path:
        return self.runtime_file.parent

    def _wallet(self) -> bytes:
        try:
            value = (self._wallet_dir() / "wallet.zip").read_bytes()
        except OSError as exc:
            raise AutonomousProvisionError("Autonomous wallet is unavailable") from exc
        if not value:
            raise AutonomousProvisionError("Autonomous wallet is empty")
        return value

    def _connect(self, runtime: dict[str, str | int]):
        import oracledb

        return oracledb.connect(
            user=str(runtime["operator_username"]),
            password=str(runtime["operator_password"]),
            dsn=str(runtime["dsn"]),
            config_dir=str(self._wallet_dir()),
            wallet_location=str(self._wallet_dir()),
            wallet_password=str(runtime["wallet_password"]),
        )

    def _users(self) -> dict[str, dict[str, str]]:
        if not self.state_file.is_file():
            return {}
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise AutonomousProvisionError("Participant database credential state is invalid") from exc
        if not isinstance(value, dict) or any(
            not isinstance(key, str)
            or not isinstance(credentials, dict)
            or set(credentials) != {"owner_password", "reader_password"}
            or any(not isinstance(item, str) or not item for item in credentials.values())
            for key, credentials in value.items()
        ):
            raise AutonomousProvisionError("Participant database credential state is invalid")
        return value

    def _write_users(self, users: dict[str, dict[str, str]]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(users, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.state_file)
        self.state_file.chmod(0o600)


def _password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(32))
        if any(c.islower() for c in value) and any(c.isupper() for c in value) and any(c.isdigit() for c in value):
            return value
