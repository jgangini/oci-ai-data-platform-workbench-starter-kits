"""Assemble readable native Python tasks from the maintained implementation."""

from __future__ import annotations

import hashlib
import inspect
import pprint
from uuid import UUID

from . import catalog, delta, inventory, inventory_writer, legacy_inventory, native_sync
from . import repository, service_transport, sync_inventory, task_arguments
from .schema import NAMESPACE, load_model


def _validate_runtime_bindings(config: dict) -> None:
    required = {"region", "platform_id", "workspace_key", "credential_name", "identity_sha256",
                "storage_root", "job_key", "source_scopes"}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError("Only explicit non-secret installation bindings are accepted")
    service_transport.MetadataTransport(config, None, None)
    for name in ("job_key", "workspace_key"):
        if str(UUID(config[name])) != config[name]:
            raise ValueError("Native installation keys must be canonical UUIDs")
    if not isinstance(config["credential_name"], str) or not 1 <= len(config["credential_name"]) <= 128:
        raise ValueError("An existing runtime credential reference is required")
    if not isinstance(config["identity_sha256"], str) or not service_transport.re.fullmatch(r"[0-9a-f]{64}", config["identity_sha256"]):
        raise ValueError("An expected runtime identity binding is required")
    delta.table_location(config["storage_root"], "AI_DG_SCOPE")
    _validate_source_scopes(config["source_scopes"])


def _validate_source_scopes(scopes: list[dict]) -> None:
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= 32:
        raise ValueError("An explicit bounded source scope set is required")
    seen = set()
    for scope in scopes:
        if not isinstance(scope, dict) or set(scope) != {"catalog_key", "schema_key"}:
            raise ValueError("Invalid source scope binding")
        selected = catalog.CatalogScope(**scope)
        if not selected.schema_key.startswith(selected.catalog_key + "."):
            raise ValueError("The source schema must belong to its catalog")
        key = (selected.catalog_key, selected.schema_key)
        if key in seen:
            raise ValueError("Duplicate source scope")
        seen.add(key)


def inventory_task_source(config: dict) -> bytes:
    _validate_runtime_bindings(config)
    symbols = (
        delta.qualified_table, delta.table_location, delta._verify_table,
        repository.checked_rows, repository._value, repository._spark_schema,
        repository.DeltaRepository, repository._comparison_frame, repository._comparable,
        repository._restore_timestamps, inventory.canonical, inventory.identity,
        inventory.description_origin, inventory.InventoryRecords, inventory.table_records,
        inventory._check_columns, inventory._asset_id, inventory._add_observation,
        catalog.CatalogScope, catalog.CatalogPage, catalog._check_scope, catalog._text, catalog.is_governance_control_table,
        catalog.is_redacted_metadata,
        catalog.normalize_columns, catalog.CatalogReader,
        inventory_writer.persist_inventory, inventory_writer._one, inventory_writer._existing,
        inventory_writer._retain_original, inventory_writer._save_current, inventory_writer._validate_records,
        inventory_writer._asset_transitions, inventory_writer._current_projections, inventory_writer._work_to_reopen,
        inventory_writer._reopenable_work,
        inventory_writer._validate_asset_versions, inventory_writer._validate_projections,
        inventory_writer._validate_evidence, inventory_writer._validate_work,
        legacy_inventory.legacy_column_ids, legacy_inventory._legacy_matches,
        service_transport.runtime_signer, service_transport.MetadataTransport,
        task_arguments.TaskArguments, task_arguments.parse_task_arguments,
        sync_inventory.synchronize_scope, sync_inventory._flush_inventory, sync_inventory._inventory_chunks,
        sync_inventory._sync_context, sync_inventory._collect_inventory, sync_inventory._catalog_pages,
        sync_inventory._table_observation, sync_inventory._enqueue_inventory, sync_inventory._close_inventory_run,
        native_sync.admit_native_sync, native_sync.prepare_sync_context,
        native_sync.load_legacy_column_ids, native_sync.run_native_inventory,
    )
    body = "\n\n".join(inspect.getsource(symbol) for symbol in symbols)
    revision = hashlib.sha256(body.encode("utf-8")).hexdigest()
    header = (
        '"""AI_DG inventory task. Maintained source: app/ai_data_governance/. No catalog writes."""\n'
        "from __future__ import annotations\n\n"
        "import argparse\nimport hashlib\nimport json\nimport re\n"
        "from dataclasses import dataclass, field\nfrom datetime import datetime, timedelta, timezone\n"
        "from typing import Any, Callable, Iterator, Mapping\nfrom urllib.parse import quote, unquote\n"
        "from uuid import UUID, uuid4\n\n"
        f"NAMESPACE = {NAMESPACE!r}\nCONFIG = {pprint.pformat(config, sort_dicts=True, width=100)}\n"
        f"COMPONENT_REVISION = {revision!r}\nMODEL = {pprint.pformat(load_model(), sort_dicts=False, width=100)}\n\n"
        "def load_model():\n    return MODEL\n\n"
    )
    footer = (
        '\n\nif __name__ == "__main__":\n'
        "    try:\n        print(json.dumps(run_native_inventory(CONFIG, COMPONENT_REVISION, aidputils.secrets.get), sort_keys=True))\n"
        "    except Exception as exc:\n"
        "        print(json.dumps({'phase':'sync_inventory','status':'FAILED','error_type':type(exc).__name__}))\n"
        "        raise RuntimeError('Governance inventory failed; reconcile this task attempt before retrying') from None\n"
    )
    source = (header + body + footer).encode("utf-8")
    compile(source, "sync_inventory.py", "exec")
    return source
