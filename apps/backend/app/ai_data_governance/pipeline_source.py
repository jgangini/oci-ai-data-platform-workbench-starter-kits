"""Generate readable Python entries; no encoded bundle or runtime source loader."""

from __future__ import annotations

import ast
import hashlib
import inspect
import pprint

from . import catalog, catalog_metadata, code_context, contracts, delta, documentary_graph, documentary_hooks
from . import documentation, inventory, inventory_writer, journal, legacy_inventory, lineage
from . import materialize, model_output, native_pipeline, native_sync, outcome_receipts
from . import catalog_discovery, publication, publication_context, replication, repository, service_transport
from . import sync_inventory, task_arguments, workflow_receipts, workflow_staging
from .runtime_source import _validate_runtime_bindings
from .schema import NAMESPACE, load_model


def pipeline_task_source(binding: dict, task_key: str) -> bytes:
    required = {"model_id", "compartment_id", "documentation_agent_key", "documentary_revision"}
    if not required.issubset(binding) or task_key not in {"sync_inventory", "publish_metadata", "replicate_model"}:
        raise ValueError("Explicit pipeline and documentary bindings are required")
    _validate_runtime_bindings({k: v for k, v in binding.items() if k not in required})
    modules = (contracts, inventory, model_output, documentation, delta, repository, catalog,
        catalog_metadata, inventory_writer, legacy_inventory, service_transport, task_arguments,
        sync_inventory, native_sync, lineage, code_context, journal, documentary_graph, documentary_hooks,
        outcome_receipts, materialize, publication_context, publication, replication,
        workflow_receipts, workflow_staging, catalog_discovery, native_pipeline)
    selected = _pipeline_symbols(modules, "pipeline_inventory" if task_key == "sync_inventory" else "pipeline_effects")
    pieces, names = [], set()
    for module in modules:
        source = inspect.getsource(module)
        pieces.append("# Maintained module: " + module.__name__ + "\n")
        for node in ast.parse(source).body:
            if isinstance(node, ast.ImportFrom) and (node.level or node.module == "__future__"):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name not in selected:
                    continue
                if node.name in names:
                    raise ValueError("Assembled runtime has a duplicate symbol")
                names.add(node.name)
            first_line = min([node.lineno, *[d.lineno for d in getattr(node, "decorator_list", [])]])
            pieces.append("\n".join(source.splitlines()[first_line - 1:node.end_lineno]) + "\n")
    body = "\n".join(pieces)
    revision = hashlib.sha256(body.encode()).hexdigest()
    header = ('"""AI_DG native ' + task_key + ' task; maintained app/ai_data_governance sources."""\n'
        'from __future__ import annotations\nfrom typing import Any, Callable, Iterator, Mapping\n'
        'from dataclasses import dataclass, field, asdict\nfrom datetime import datetime, timedelta, timezone\n'
        f'NAMESPACE = {NAMESPACE!r}\nCONFIG = {pprint.pformat(binding)}\n'
        f'MODEL = {pprint.pformat(load_model())}\nCOMPONENT_REVISION = {revision!r}\n'
        'def load_model():\n    return MODEL\n'
        'def descriptions():\n    return {t["name"]: {"table": t["grain"], **{c["name"]: c["comment"] for c in t["columns"]}} for t in MODEL["tables"]}\n')
    footer = ('\nif __name__ == "__main__":\n    try:\n'
        f'        args = parse_task_arguments()\n        if args.task_key != {task_key!r}:\n            raise ValueError("The Python entry does not match the task")\n'
        '        print(canonical(run_native_pipeline(CONFIG, COMPONENT_REVISION, aidputils.secrets.get, '
        + ('pipeline_inventory' if task_key == 'sync_inventory' else 'pipeline_effects') + ')))\n'
        '    except Exception as exc:\n        print(canonical(pipeline_failure_report(' + repr(task_key) + ', exc)))\n'
        '        raise RuntimeError("Governance task failed; inspect its durable phase receipts") from None\n')
    result = (header + body + footer).encode()
    compile(result, task_key + ".py", "exec")
    return result


def _pipeline_symbols(modules, runner):
    definitions = {}
    for module in modules:
        for node in ast.parse(inspect.getsource(module)).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in definitions:
                    raise ValueError("Assembled runtime has a duplicate symbol")
                definitions[node.name] = node
    selected, pending = set(), [runner, "run_native_pipeline", "canonical", "parse_task_arguments", "pipeline_failure_report"]
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        selected.add(name)
        node = definitions[name]
        pending.extend(n.id for n in ast.walk(node) if isinstance(n, ast.Name) and n.id in definitions and n.id not in selected)
    return selected
