"""Shared OCI provisioning results and errors, independent of service clients."""

from __future__ import annotations

import re
from dataclasses import dataclass


class AidpProvisionPending(Exception):
    def __init__(self, message: str, phase: str = "content") -> None:
        super().__init__(message)
        self.phase = phase


class AidpProvisionError(Exception):
    pass


class AidpRequestError(AidpProvisionError):
    """Transport metadata without request bodies, response bodies or credentials."""

    def __init__(self, message: str, *, method: str, path: str, phase: str,
                 status_code: int, request_id: str) -> None:
        super().__init__(message)
        self.diagnostic = {
            "method": method.upper(), "phase": phase, "status_code": status_code,
            "endpoint": re.sub(r"(/workspaces/)[^/]+", r"\1{workspace}", path.split("?", 1)[0])[:240],
            "request_id": request_id if re.fullmatch(r"[A-Za-z0-9:/_.=-]{1,256}", request_id) else "",
        }


class AidpProvisionConflict(Exception):
    pass


@dataclass(frozen=True, slots=True)
class UserMaterial:
    email: str
    lab_id: str
    participant_key: str
    workspace_path: str
    job_name: str
    pack_version: str = "1.0.0"
    phase: str = "active"
    participant_code: int | None = None


class IdentityConflict(Exception):
    pass


class IdentityPending(Exception):
    pass


class IdentityRejected(Exception):
    pass


class IdentityRace(Exception):
    pass


@dataclass(slots=True)
class RegistrationResult:
    status: str
    user_id: str
    user_ocid: str
    email: str
    was_developer: bool = False
