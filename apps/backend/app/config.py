from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .security import hash_secret


@dataclass(frozen=True, slots=True)
class Settings:
    admin_username: str = "admin"
    admin_password_hash: str = ""
    registration_code_hash: str = ""
    identity_domain_url: str = ""
    developer_group_id: str = ""
    pending_group_id: str = ""
    aidp_workbench_url: str = ""
    aidp_platform_id: str = ""
    aidp_workspace_name: str = ""
    aidp_region: str = ""
    compartment_id: str = ""
    autonomous_database_id: str = ""
    autonomous_runtime_file: str = "/etc/aidp-lab/autonomous/runtime.json"
    agent_model_id: str = ""
    oci_config_file: str = "/etc/aidp-lab/oci/config"
    objectstorage_namespace: str = ""
    bucket_name: str = ""
    aidp_settings_file: str = "/var/lib/aidp-lab/settings.json"
    lab_marker: str = "aidp-lab"
    session_secret_file: str = "/var/lib/aidp-lab/session.key"
    cookie_secure: bool = True
    local_development_mode: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            admin_username=os.getenv("ADMIN_USERNAME", "admin"),
            admin_password_hash=os.getenv("ADMIN_PASSWORD_HASH", ""),
            registration_code_hash=os.getenv("REGISTRATION_CODE_HASH", ""),
            identity_domain_url=os.getenv("IDENTITY_DOMAIN_URL", "").rstrip("/"),
            developer_group_id=os.getenv("IDENTITY_DEVELOPER_GROUP_ID", ""),
            pending_group_id=os.getenv("IDENTITY_PENDING_GROUP_ID", ""),
            aidp_workbench_url=os.getenv("AIDP_WORKBENCH_URL", ""),
            aidp_platform_id=os.getenv("AIDP_PLATFORM_ID", ""),
            aidp_workspace_name=os.getenv("AIDP_WORKSPACE_NAME", ""),
            aidp_region=os.getenv("AIDP_REGION", ""),
            compartment_id=os.getenv("COMPARTMENT_ID", ""),
            autonomous_database_id=os.getenv("AUTONOMOUS_DATABASE_ID", ""),
            autonomous_runtime_file=os.getenv(
                "AUTONOMOUS_RUNTIME_FILE",
                "/etc/aidp-lab/autonomous/runtime.json",
            ),
            agent_model_id=os.getenv("AGENT_MODEL_ID", ""),
            oci_config_file=os.getenv("OCI_CONFIG_FILE", "/etc/aidp-lab/oci/config"),
            objectstorage_namespace=os.getenv("OBJECTSTORAGE_NAMESPACE", ""),
            bucket_name=os.getenv("BUCKET_NAME", ""),
            aidp_settings_file=os.getenv("AIDP_SETTINGS_FILE", "/var/lib/aidp-lab/settings.json"),
            lab_marker=os.getenv("LAB_MARKER", "aidp-lab"),
            session_secret_file=os.getenv("SESSION_SECRET_FILE", "/var/lib/aidp-lab/session.key"),
            cookie_secure=os.getenv("COOKIE_SECURE", "true").lower() not in {"0", "false", "no"},
            local_development_mode=os.getenv("LOCAL_DEVELOPMENT_MODE", "false").lower() in {"1", "true", "yes"},
        )

    def identity_ready(self) -> bool:
        if self.local_development_mode:
            return True
        return all(
            (
                self.identity_domain_url,
                self.developer_group_id,
                self.pending_group_id,
                self.oci_config_file,
            )
        )

    def aidp_ready(self) -> bool:
        return self.local_development_mode or all(
            (
                self.aidp_platform_id,
                self.aidp_workspace_name,
                self.aidp_region,
                self.oci_config_file,
                self.objectstorage_namespace,
                self.bucket_name,
            )
        )


class SettingsStore:
    """Persists administrator-edited settings; registration codes remain PBKDF2 verifiers."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()

    def get_admin_settings(self) -> dict[str, str | bool]:
        values = self._load()
        return {
            "aidp_url": values["aidp_workbench_url"],
            "aidp_platform_id": self._settings.aidp_platform_id,
            "registration_code_configured": bool(values["registration_code_hash"]),
        }

    def get_registration_code_hash(self) -> str:
        return self._load()["registration_code_hash"]

    def get_workbench_url(self) -> str:
        return self._load()["aidp_workbench_url"]

    def update(self, aidp_url: str | None, registration_code: str | None) -> dict[str, str | bool]:
        if aidp_url is None and registration_code is None:
            raise ValueError("Update the AI Data Platform URL or the lab registration code")
        values = self._load()
        if aidp_url is not None:
            normalized = aidp_url.strip()
            if not _valid_workbench_url(normalized):
                raise ValueError("Enter a valid HTTPS Oracle AI Data Platform Workbench URL")
            values["aidp_workbench_url"] = normalized
        if registration_code is not None:
            values["registration_code_hash"] = hash_secret(registration_code)
        self._write(values)
        return self.get_admin_settings()

    def participant_code(self, email: str) -> int:
        normalized = email.strip().casefold()
        if normalized.count("@") != 1 or any(character.isspace() for character in normalized):
            raise ValueError("A valid participant email is required")
        with self._lock:
            values = self._load()
            codes = values["participant_codes"]
            existing = codes.get(normalized)
            if isinstance(existing, int) and existing >= 101:
                return existing
            code = values["next_participant_code"]
            codes[normalized] = code
            values["next_participant_code"] = code + 1
            self._write(values)
            return code

    def _load(self) -> dict[str, object]:
        values = {
            "aidp_workbench_url": self._settings.aidp_workbench_url
            if _valid_workbench_url(self._settings.aidp_workbench_url)
            else "",
            "registration_code_hash": self._settings.registration_code_hash,
            "next_participant_code": 101,
            "participant_codes": {},
        }
        path = Path(self._settings.aidp_settings_file)
        if path.is_file():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
                aidp_url = stored.get("aidp_workbench_url", "")
                registration_code_hash = stored.get("registration_code_hash", "")
                if isinstance(aidp_url, str) and _valid_workbench_url(aidp_url):
                    values["aidp_workbench_url"] = aidp_url
                if isinstance(registration_code_hash, str) and registration_code_hash:
                    values["registration_code_hash"] = registration_code_hash
                next_code = stored.get("next_participant_code")
                participant_codes = stored.get("participant_codes")
                if isinstance(participant_codes, dict):
                    valid_codes = {
                        str(email).casefold(): code
                        for email, code in participant_codes.items()
                        if isinstance(email, str)
                        and isinstance(code, int)
                        and not isinstance(code, bool)
                        and code >= 101
                    }
                    values["participant_codes"] = valid_codes
                    minimum_next = max(valid_codes.values(), default=100) + 1
                    if isinstance(next_code, int) and not isinstance(next_code, bool):
                        values["next_participant_code"] = max(101, next_code, minimum_next)
                    else:
                        values["next_participant_code"] = minimum_next
            except (OSError, ValueError, TypeError):
                pass

        return values

    def _write(self, values: dict[str, object]) -> None:
        path = Path(self._settings.aidp_settings_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values) + "\n", encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _valid_workbench_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and parsed.hostname is not None and (
        parsed.hostname.endswith(".datalake.oci.oraclecloud.com") or parsed.hostname.endswith(".example.invalid")
    )
