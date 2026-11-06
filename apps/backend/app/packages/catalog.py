"""Resolve participant kits; validate their immutable assets."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
KITS_ROOT = APP_ROOT / "kits"
CATALOG_PATH = Path(__file__).with_name("catalog.json")
LAB_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{1,31}")
PACK_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
MEDALLION_LAYERS = frozenset({"landing", "bronze", "silver", "gold"})


class PackageError(ValueError):
    """Raised when a checked-in lab package violates its contract."""


def package_root(package_id: str) -> Path:
    if not isinstance(package_id, str) or LAB_ID_PATTERN.fullmatch(package_id) is None:
        raise PackageError("Invalid package_id")
    return KITS_ROOT / package_id


def package_metadata_path(package_id: str) -> Path:
    root = package_root(package_id)
    return root / "lab.json"


@dataclass(frozen=True)
class PackageAsset:
    name: str
    path: Path
    sha256: str
    row_count: int | None = None
    task_key: str | None = None
    depends_on: tuple[str, ...] = ()

    def read_bytes(self) -> bytes:
        content = self.path.read_bytes()
        if _sha256(content) != self.sha256:
            raise PackageError(f"SHA-256 changed while reading {self.name}")
        return content


@dataclass(frozen=True)
class Package:
    lab_id: str
    display_name: str
    pack_version: str
    status: str
    kind: str
    scope: str
    installation_modes: tuple[str, ...]
    pack_sha256: str
    datasets: tuple[PackageAsset, ...]
    notebooks: tuple[PackageAsset, ...]
    tables: dict[str, tuple[str, ...]]
    formats: dict[str, str]
    expected_results: dict[str, Any]

    @property
    def available(self) -> bool:
        return self.status == "available"


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError(f"Invalid lab metadata: {path.name}") from exc
    if not isinstance(value, dict):
        raise PackageError(f"Lab metadata must be an object: {path.name}")
    return value


def _safe_name(value: Any, label: str) -> str:
    name = str(value or "")
    if not name or Path(name).name != name or name in {".", ".."}:
        raise PackageError(f"Invalid {label}")
    return name


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _dataset_asset(
    path: Path, raw: dict[str, Any], digest: str, content: bytes
) -> PackageAsset:
    if _safe_name(raw.get("name"), "dataset name") != path.stem:
        raise PackageError(f"Dataset name does not match its file: {path.name}")
    try:
        rows = list(csv.reader(io.StringIO(content.decode("utf-8"))))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise PackageError(f"Invalid CSV asset: {path.name}") from exc
    if not rows or "participant_key" in rows[0]:
        raise PackageError(f"Canonical CSV must omit participant_key: {path.name}")
    row_count = raw.get("row_count")
    if not isinstance(row_count, int) or row_count < 0 or len(rows) - 1 != row_count:
        raise PackageError(f"Row count mismatch for {path.name}")
    return PackageAsset(path.name, path, digest, row_count=row_count)


def _notebook_asset(
    path: Path, raw: dict[str, Any], digest: str, content: bytes
) -> PackageAsset:
    try:
        notebook = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PackageError(f"Invalid notebook JSON: {path.name}") from exc
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        raise PackageError(f"Invalid notebook contract: {path.name}")
    task_key = str(raw.get("task_key") or "")
    depends_on = raw.get("depends_on", [])
    if not re.fullmatch(r"[A-Za-z0-9_]{1,100}", task_key) or not isinstance(depends_on, list):
        raise PackageError(f"Invalid notebook task metadata: {path.name}")
    return PackageAsset(
        path.name,
        path,
        digest,
        task_key=task_key,
        depends_on=tuple(map(str, depends_on)),
    )


def _declared_asset(root: Path, raw: Any, kind: str) -> PackageAsset:
    if not isinstance(raw, dict):
        raise PackageError(f"Invalid {kind} entry")
    name = _safe_name(raw.get("file"), f"{kind} file")
    path = root / name
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise PackageError(f"Missing {kind} asset: {name}") from exc
    digest = str(raw.get("sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or _sha256(content) != digest:
        raise PackageError(f"SHA-256 mismatch for {name}")
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
) -> tuple[str, str, str, str]:
    if metadata.get("schema_version") != 1 or metadata.get("lab_id") != lab_id:
        raise PackageError(f"Invalid lab identity: {lab_id}")
    status = str(metadata.get("status") or "")
    if status not in {"available", "planned"}:
        raise PackageError(f"Invalid lab status: {lab_id}")
    if require_available and status != "available":
        raise PackageError(f"Lab {lab_id} is not available yet")
    kind = str(metadata.get("kind") or "data_pipeline")
    if kind != "data_pipeline":
        raise PackageError(f"Invalid lab kind: {lab_id}")
    pack_version = str(metadata.get("pack_version") or "")
    if PACK_VERSION_PATTERN.fullmatch(pack_version) is None:
        raise PackageError(f"Invalid pack version: {lab_id}")
    digest = str(metadata.get("pack_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or digest != _pack_hash(metadata):
        raise PackageError(f"Pack hash mismatch: {lab_id}")
    return status, kind, pack_version, digest


def _pack_assets(
    root: Path, metadata: dict[str, Any], status: str, kind: str, lab_id: str
) -> tuple[tuple[PackageAsset, ...], tuple[PackageAsset, ...]]:
    datasets = tuple(
        _declared_asset(root / "source", item, "dataset")
        for item in metadata.get("datasets", [])
    )
    notebooks = tuple(
        _declared_asset(root / "notebooks", item, "notebook")
        for item in metadata.get("notebooks", [])
    )
    if status == "available" and kind == "data_pipeline" and (not datasets or not notebooks):
        raise PackageError(f"Available lab has no assets: {lab_id}")
    if len({item.name for item in datasets}) != len(datasets):
        raise PackageError(f"Duplicate dataset file: {lab_id}")
    if len({item.name for item in notebooks}) != len(notebooks):
        raise PackageError(f"Duplicate notebook file: {lab_id}")
    task_keys = [item.task_key for item in notebooks]
    if len(task_keys) != len(set(task_keys)):
        raise PackageError(f"Duplicate task key: {lab_id}")
    seen: set[str | None] = set()
    for item in notebooks:
        if any(dependency not in seen for dependency in item.depends_on):
            raise PackageError(f"Notebook dependency must reference an earlier task: {item.name}")
        seen.add(item.task_key)
    return datasets, notebooks


def _pack_tables(
    metadata: dict[str, Any], status: str, kind: str, lab_id: str
) -> dict[str, tuple[str, ...]]:
    raw_tables = metadata.get("tables", {})
    if not isinstance(raw_tables, dict) or (
        status == "available" and kind == "data_pipeline" and set(raw_tables) != MEDALLION_LAYERS
    ):
        raise PackageError(f"Invalid table inventory: {lab_id}")
    return {
        str(layer): tuple(_safe_name(name, "table name") for name in names)
        for layer, names in raw_tables.items()
        if isinstance(names, list)
    }


def _pack_expected_results(
    metadata: dict[str, Any], status: str, kind: str, lab_id: str
) -> dict[str, Any]:
    expected = metadata.get("expected_results", {})
    if not isinstance(expected, dict) or (
        status == "available" and kind == "data_pipeline"
        and set(expected)
        != {"source_row_counts", "business_aggregates", "quality", "lineage"}
    ):
        raise PackageError(f"Invalid expected results: {lab_id}")
    return expected


def _pack_formats(
    metadata: dict[str, Any], status: str, kind: str, lab_id: str
) -> dict[str, str]:
    raw_formats = metadata.get("formats", {})
    if not isinstance(raw_formats, dict):
        raise PackageError(f"Invalid table formats: {lab_id}")
    if not raw_formats:
        return {}
    formats = {str(layer): str(value).upper() for layer, value in raw_formats.items()}
    if (
        status == "available" and kind == "data_pipeline"
        and set(formats) != MEDALLION_LAYERS
        or any(value not in {"CSV", "DELTA", "ICEBERG"} for value in formats.values())
    ):
        raise PackageError(f"Invalid table formats: {lab_id}")
    return formats


def load_package(lab_id: str, *, require_available: bool = True) -> Package:
    if LAB_ID_PATTERN.fullmatch(lab_id) is None:
        raise PackageError("Invalid lab_id")
    root = package_root(lab_id)
    metadata = _object(package_metadata_path(lab_id))
    status, kind, pack_version, digest = _pack_contract(
        metadata, lab_id, require_available
    )
    datasets, notebooks = _pack_assets(root, metadata, status, kind, lab_id)
    scope = str(metadata.get("scope") or "participant")
    installation_modes = tuple(map(str, metadata.get("installation_modes") or ["laboratory", "production"]))
    if scope != "participant" or any(
        mode not in {"laboratory", "production"} for mode in installation_modes
    ):
        raise PackageError(f"Invalid lab scope: {lab_id}")
    return Package(
        lab_id=lab_id,
        display_name=str(metadata.get("display_name") or lab_id.title()),
        pack_version=pack_version,
        status=status,
        kind=kind,
        scope=scope,
        installation_modes=installation_modes,
        pack_sha256=digest,
        datasets=datasets,
        notebooks=notebooks,
        tables=_pack_tables(metadata, status, kind, lab_id),
        formats=_pack_formats(metadata, status, kind, lab_id),
        expected_results=_pack_expected_results(metadata, status, kind, lab_id),
    )


def _catalog_manifest() -> tuple[tuple[str, ...], dict[str, str]]:
    catalog = _object(CATALOG_PATH)
    lab_ids = catalog.get("labs")
    if catalog.get("schema_version") != 1 or not isinstance(lab_ids, list):
        raise PackageError("Invalid lab catalog")
    ids = tuple(map(str, lab_ids))
    if len(ids) != len(set(ids)):
        raise PackageError("Lab catalog contains duplicates")
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
        raise PackageError("Lab catalog descriptions must cover every lab")
    return ids, {lab_id: descriptions[lab_id].strip() for lab_id in ids}


def package_catalog() -> tuple[Package, ...]:
    ids, _ = _catalog_manifest()
    return tuple(load_package(lab_id, require_available=False) for lab_id in ids)


def available_kit_ids() -> tuple[str, ...]:
    return tuple(
        pack.lab_id
        for pack in package_catalog()
        if pack.available and pack.scope == "participant"
    )


def public_kit_catalog() -> list[dict[str, Any]]:
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
        for pack in package_catalog()
        if pack.scope == "participant"
    ]
