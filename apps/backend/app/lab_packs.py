"""Load and validate versioned, immutable AIDP laboratory assets."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LABS_ROOT = Path(__file__).with_name("labs")
LAB_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{1,31}")
PACK_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
MEDALLION_LAYERS = frozenset({"landing", "bronze", "silver", "gold"})


class LabPackError(ValueError):
    """Raised when a checked-in lab package violates its contract."""


@dataclass(frozen=True)
class LabAsset:
    name: str
    path: Path
    sha256: str
    row_count: int | None = None
    task_key: str | None = None
    depends_on: tuple[str, ...] = ()

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()


@dataclass(frozen=True)
class LabPack:
    lab_id: str
    display_name: str
    pack_version: str
    status: str
    pack_sha256: str
    datasets: tuple[LabAsset, ...]
    notebooks: tuple[LabAsset, ...]
    tables: dict[str, tuple[str, ...]]
    expected_results: dict[str, Any]

    @property
    def available(self) -> bool:
        return self.status == "available"


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LabPackError(f"Invalid lab metadata: {path.name}") from exc
    if not isinstance(value, dict):
        raise LabPackError(f"Lab metadata must be an object: {path.name}")
    return value


def _safe_name(value: Any, label: str) -> str:
    name = str(value or "")
    if not name or Path(name).name != name or name in {".", ".."}:
        raise LabPackError(f"Invalid {label}")
    return name


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _dataset_asset(
    path: Path, raw: dict[str, Any], digest: str, content: bytes
) -> LabAsset:
    if _safe_name(raw.get("name"), "dataset name") != path.stem:
        raise LabPackError(f"Dataset name does not match its file: {path.name}")
    try:
        rows = list(csv.reader(io.StringIO(content.decode("utf-8"))))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise LabPackError(f"Invalid CSV asset: {path.name}") from exc
    if not rows or "participant_key" in rows[0]:
        raise LabPackError(f"Canonical CSV must omit participant_key: {path.name}")
    row_count = raw.get("row_count")
    if not isinstance(row_count, int) or row_count < 0 or len(rows) - 1 != row_count:
        raise LabPackError(f"Row count mismatch for {path.name}")
    return LabAsset(path.name, path, digest, row_count=row_count)


def _notebook_asset(
    path: Path, raw: dict[str, Any], digest: str, content: bytes
) -> LabAsset:
    try:
        notebook = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LabPackError(f"Invalid notebook JSON: {path.name}") from exc
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        raise LabPackError(f"Invalid notebook contract: {path.name}")
    task_key = str(raw.get("task_key") or "")
    depends_on = raw.get("depends_on", [])
    if not re.fullmatch(r"[A-Za-z0-9_]{1,100}", task_key) or not isinstance(depends_on, list):
        raise LabPackError(f"Invalid notebook task metadata: {path.name}")
    return LabAsset(
        path.name,
        path,
        digest,
        task_key=task_key,
        depends_on=tuple(map(str, depends_on)),
    )


def _declared_asset(root: Path, raw: Any, kind: str) -> LabAsset:
    if not isinstance(raw, dict):
        raise LabPackError(f"Invalid {kind} entry")
    name = _safe_name(raw.get("file"), f"{kind} file")
    path = root / name
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise LabPackError(f"Missing {kind} asset: {name}") from exc
    digest = str(raw.get("sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or _sha256(content) != digest:
        raise LabPackError(f"SHA-256 mismatch for {name}")
    return (
        _dataset_asset(path, raw, digest, content)
        if kind == "dataset"
        else _notebook_asset(path, raw, digest, content)
    )


def _pack_hash(metadata: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in metadata.items() if key != "pack_sha256"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(canonical)


def _pack_contract(
    metadata: dict[str, Any], lab_id: str, require_available: bool
) -> tuple[str, str, str]:
    if metadata.get("schema_version") != 1 or metadata.get("lab_id") != lab_id:
        raise LabPackError(f"Invalid lab identity: {lab_id}")
    status = str(metadata.get("status") or "")
    if status not in {"available", "planned"}:
        raise LabPackError(f"Invalid lab status: {lab_id}")
    if require_available and status != "available":
        raise LabPackError(f"Lab {lab_id} is not available yet")
    pack_version = str(metadata.get("pack_version") or "")
    if PACK_VERSION_PATTERN.fullmatch(pack_version) is None:
        raise LabPackError(f"Invalid pack version: {lab_id}")
    digest = str(metadata.get("pack_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or digest != _pack_hash(metadata):
        raise LabPackError(f"Pack hash mismatch: {lab_id}")
    return status, pack_version, digest


def _pack_assets(
    root: Path, metadata: dict[str, Any], status: str, lab_id: str
) -> tuple[tuple[LabAsset, ...], tuple[LabAsset, ...]]:
    datasets = tuple(
        _declared_asset(root / "source", item, "dataset")
        for item in metadata.get("datasets", [])
    )
    notebooks = tuple(
        _declared_asset(root / "notebooks", item, "notebook")
        for item in metadata.get("notebooks", [])
    )
    if status == "available" and (not datasets or not notebooks):
        raise LabPackError(f"Available lab has no assets: {lab_id}")
    if len({item.name for item in datasets}) != len(datasets):
        raise LabPackError(f"Duplicate dataset file: {lab_id}")
    if len({item.name for item in notebooks}) != len(notebooks):
        raise LabPackError(f"Duplicate notebook file: {lab_id}")
    task_keys = [item.task_key for item in notebooks]
    if len(task_keys) != len(set(task_keys)):
        raise LabPackError(f"Duplicate task key: {lab_id}")
    seen: set[str | None] = set()
    for item in notebooks:
        if any(dependency not in seen for dependency in item.depends_on):
            raise LabPackError(f"Notebook dependency must reference an earlier task: {item.name}")
        seen.add(item.task_key)
    return datasets, notebooks


def _pack_tables(
    metadata: dict[str, Any], status: str, lab_id: str
) -> dict[str, tuple[str, ...]]:
    raw_tables = metadata.get("tables", {})
    if not isinstance(raw_tables, dict) or (
        status == "available" and set(raw_tables) != MEDALLION_LAYERS
    ):
        raise LabPackError(f"Invalid table inventory: {lab_id}")
    return {
        str(layer): tuple(_safe_name(name, "table name") for name in names)
        for layer, names in raw_tables.items()
        if isinstance(names, list)
    }


def _pack_expected_results(
    metadata: dict[str, Any], status: str, lab_id: str
) -> dict[str, Any]:
    expected = metadata.get("expected_results", {})
    if not isinstance(expected, dict) or (
        status == "available"
        and set(expected)
        != {"source_row_counts", "business_aggregates", "quality", "lineage"}
    ):
        raise LabPackError(f"Invalid expected results: {lab_id}")
    return expected


def load_lab_pack(lab_id: str, *, require_available: bool = True) -> LabPack:
    if LAB_ID_PATTERN.fullmatch(lab_id) is None:
        raise LabPackError("Invalid lab_id")
    root = LABS_ROOT / lab_id
    metadata = _object(root / "lab.json")
    status, pack_version, digest = _pack_contract(
        metadata, lab_id, require_available
    )
    datasets, notebooks = _pack_assets(root, metadata, status, lab_id)
    return LabPack(
        lab_id=lab_id,
        display_name=str(metadata.get("display_name") or lab_id.title()),
        pack_version=pack_version,
        status=status,
        pack_sha256=digest,
        datasets=datasets,
        notebooks=notebooks,
        tables=_pack_tables(metadata, status, lab_id),
        expected_results=_pack_expected_results(metadata, status, lab_id),
    )


def _catalog_manifest() -> tuple[tuple[str, ...], dict[str, str]]:
    catalog = _object(LABS_ROOT / "catalog.json")
    lab_ids = catalog.get("labs")
    if catalog.get("schema_version") != 1 or not isinstance(lab_ids, list):
        raise LabPackError("Invalid lab catalog")
    ids = tuple(map(str, lab_ids))
    if len(ids) != len(set(ids)):
        raise LabPackError("Lab catalog contains duplicates")
    descriptions = catalog.get("descriptions")
    if (
        not isinstance(descriptions, dict)
        or set(descriptions) != set(ids)
        or any(
            not isinstance(descriptions[lab_id], str)
            or not descriptions[lab_id].strip()
            for lab_id in ids
        )
    ):
        raise LabPackError("Lab catalog descriptions must cover every lab")
    return ids, {lab_id: descriptions[lab_id].strip() for lab_id in ids}


def lab_catalog() -> tuple[LabPack, ...]:
    ids, _ = _catalog_manifest()
    return tuple(load_lab_pack(lab_id, require_available=False) for lab_id in ids)


def available_lab_ids() -> tuple[str, ...]:
    return tuple(pack.lab_id for pack in lab_catalog() if pack.available)


def public_lab_catalog() -> list[dict[str, Any]]:
    _, descriptions = _catalog_manifest()
    return [
        {
            "lab_id": pack.lab_id,
            "display_name": pack.display_name,
            "description": descriptions[pack.lab_id],
            "pack_version": pack.pack_version,
            "status": pack.status,
            "available": pack.available,
        }
        for pack in lab_catalog()
    ]
