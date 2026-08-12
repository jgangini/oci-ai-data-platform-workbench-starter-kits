"""Stable AIDP naming conventions shared by provisioning and lab packages."""

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


def workspace_participant_root(email: str) -> str:
    return f"{WORKSPACE_ROOT}/{participant_folder(email)}"


def workspace_root(email: str, lab_id: str) -> str:
    if lab_id not in available_lab_ids():
        raise ValueError("Choose an available lab")
    return f"{workspace_participant_root(email)}/{lab_id}"


def table_name(participant_key: str, lab_id: str, dataset: str) -> str:
    prefix = f"{lab_id}_"
    return f"{participant_key}_{dataset if dataset.startswith(prefix) else prefix + dataset}"
