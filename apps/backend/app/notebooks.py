"""Stable AIDP naming conventions shared by provisioning and lab packages."""

import re
from urllib.parse import quote

from .lab_packs import available_lab_ids


LAYER_PREFIXES = {
    "landing": "01_landing",
    "bronze": "02_bronze",
    "silver": "03_silver",
    "gold": "04_gold",
}
WORKSPACE_ROOT = "/Workspace/medallon"


def schema_name(layer: str) -> str:
    if layer not in LAYER_PREFIXES:
        raise ValueError(f"Unknown medallion layer: {layer}")
    return f"oci_{layer}"


def participant_folder(email: str) -> str:
    normalized = email.strip().casefold()
    if (
        normalized.count("@") != 1
        or len(normalized) > 254
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError("A valid participant email is required")
    return quote(normalized, safe="@._+-")


def participant_key(participant_code: int) -> str:
    if not isinstance(participant_code, int) or isinstance(participant_code, bool) or participant_code < 101:
        raise ValueError("A participant code starting at 101 is required")
    return f"u{participant_code}"


def workspace_participant_root(participant_key: str, email: str | None = None) -> str:
    if re.fullmatch(r"u_[0-9a-f]{16}", participant_key):
        # Existing v3 participants retain their opaque workspace until an explicit rebuild.
        folder = participant_key
    elif re.fullmatch(r"u[1-9][0-9]*", participant_key) and int(participant_key[1:]) >= 101 and email:
        folder = f"{participant_key}_{participant_folder(email)}"
    else:
        raise ValueError("A valid participant key is required")
    return f"{WORKSPACE_ROOT}/{folder}"


def workspace_root(participant_key: str, lab_id: str, email: str | None = None) -> str:
    if lab_id not in available_lab_ids():
        raise ValueError("Choose an available lab")
    return f"{workspace_participant_root(participant_key, email)}/{lab_id}"


def table_name(participant_key: str, lab_id: str, dataset: str) -> str:
    prefix = f"{lab_id}_"
    return f"{participant_key}_{dataset if dataset.startswith(prefix) else prefix + dataset}"
