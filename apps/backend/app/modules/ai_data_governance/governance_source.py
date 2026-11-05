"""Readable, self-contained native consultation package from maintained source."""

from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path
import pprint
import re
from uuid import UUID

from . import caller_transport, catalog, code_context, code_usage, consultation, contracts, governance_graph, inventory, lineage
from . import case_reader, central_sql, model_output, native_governance, projection_query, response_graph, service_transport
from . import activity_records, activity_commit, activity_transport, native_activity, journal, delta
from . import case_activity, case_changes, case_commit, case_confirmation, case_policy, case_readmission
from . import case_program, case_transport, native_cases, repository, case_previews
from .activity_source import activity_program_source
from .case_source import case_program_source
from .prompts import governance_prompts
from .runtime_source import _validate_source_scopes
from .schema import NAMESPACE, load_model


_RUNTIME_PREAMBLE = ('"""AI_DG central metadata runtime. Credential reference only; no embedded keys or fallback."""\n'
    'import ast\nimport asyncio\nimport hashlib\nimport json\nimport logging\nimport os\nimport re\nimport time\n'
    'from contextlib import ExitStack\nfrom copy import deepcopy\n'
    'from dataclasses import dataclass, field\nfrom datetime import datetime, timedelta, timezone\n'
    'from typing import Annotated, Any, Callable, Iterator, Literal, Mapping, TypedDict\n'
    'from threading import Event\nfrom urllib.parse import quote, unquote, urlsplit\nfrom uuid import UUID, uuid4\n'
    'from pydantic import BaseModel, ConfigDict, Field, model_validator\n'
    'Identifier = Annotated[str, Field(min_length=1, max_length=256)]\n'
    'ShortText = Annotated[str, Field(min_length=1, max_length=2000)]\n'
    'References = Annotated[list[Identifier], Field(max_length=128)]\n'
    'def load_model():\n    return MODEL\n\n')


def governance_entry_source() -> bytes:
    return (Path(__file__).resolve().parent / 'aidp/workspaces/default/files/agents/governance/governance_agent.py').read_bytes()


def _validate_governance_binding(binding):
    required = {"region", "platform_id", "workspace_key", "agent_key", "query_compute_key",
        "model_id", "compartment_id", "source_scopes", "credential_name", "identity_sha256"}
    activity_fields={"storage_root","deployment_stage","agent_code_path"}
    if not isinstance(binding, dict) or set(binding) not in (required,required|activity_fields):
        raise ValueError("Only explicit nonsecret consultation bindings are accepted")
    region = binding["region"]
    if not isinstance(region, str) or not re.fullmatch(r"[a-z]+-[a-z]+-\d+", region):
        raise ValueError("Invalid consultation region")
    if not isinstance(binding["platform_id"], str) or not re.fullmatch(
            r"ocid1\.aidataplatform\.oc1\." + re.escape(region) + r"\.[a-z0-9]+", binding["platform_id"]):
        raise ValueError("Invalid consultation platform")
    for key in ("workspace_key", "agent_key", "query_compute_key"):
        if not isinstance(binding[key], str) or str(UUID(binding[key])) != binding[key]:
            raise ValueError("Canonical native consultation keys are required")
    for key, pattern in (("model_id", r"ocid1\.generativeaimodel\.oc1\.[a-z0-9.-]+"),
            ("compartment_id", r"ocid1\.compartment\.oc1\.\.[a-z0-9]+")):
        if not isinstance(binding[key], str) or not re.fullmatch(pattern, binding[key]):
            raise ValueError("An existing consultation model and compartment are required")
    _validate_source_scopes(binding["source_scopes"])
    _validate_central_credential(binding)
    _validate_activity_binding(binding)


def _validate_activity_binding(binding):
    if "storage_root" in binding:
        delta.table_location(binding["storage_root"],"AI_DG_RUN")
        activity_records.activity_agent({**binding,"component_revision":"0"*64})
        if sum(s['schema_key']==NAMESPACE for s in binding['source_scopes']) != 1:
            raise ValueError("Shared activity requires its installed artifacts scope")


def _validate_central_credential(binding):
    if not isinstance(binding["credential_name"], str) or not 1 <= len(binding["credential_name"]) <= 128:
        raise ValueError("An existing central credential reference is required")
    if not isinstance(binding["identity_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", binding["identity_sha256"]):
        raise ValueError("The existing technical identity must be bound explicitly")


def governance_agent_source(binding):
    _validate_governance_binding(binding)
    modules = (contracts, inventory, model_output, catalog, service_transport, lineage, code_context, code_usage, caller_transport,
        central_sql, projection_query, case_reader, consultation, governance_graph, response_graph,
        case_changes, case_commit, case_activity, case_confirmation, case_policy, case_readmission,
        case_program, case_transport, native_cases, repository, case_previews, native_governance)
    program=""
    case_program_text=""
    if "storage_root" in binding:
        modules=(*modules,activity_records,activity_commit,activity_transport,native_activity,journal)
        program=activity_program_source()
        case_program_text=case_program_source()
        if any("'''" in text for text in (program,case_program_text)):
            raise ValueError("Fixed activity source cannot escape its readable literal")
    symbols = _governance_symbols(modules)
    pieces = []
    for module in modules:
        text = inspect.getsource(module)
        pieces.append("# Maintained module: " + module.__name__ + "\n")
        for node in ast.parse(text).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in symbols:
                pieces.append(ast.get_source_segment(text, node) if not node.decorator_list else
                    "\n".join(text.splitlines()[min(d.lineno for d in node.decorator_list) - 1:node.end_lineno]))
    body = "\n\n".join(pieces)
    prompts = governance_prompts()
    behavior = {"prompts": prompts, "namespace": NAMESPACE,
        "read_operations": sorted(governance_graph.READ_OPERATIONS), "projection_fields": sorted(consultation.PROJECTION_FIELDS),
        "activity_program":program, "case_program":case_program_text, "model":load_model(),
        "case_modes":sorted(case_program.CASE_MODES), "confirmation_actions":case_confirmation.CASE_CONFIRM_ACTIONS,
        "case_request_fields":sorted(case_confirmation.CASE_REQUEST_FIELDS)}
    revision = hashlib.sha256(_RUNTIME_PREAMBLE.encode() + body.encode()
        + inventory.canonical(behavior).encode() + governance_entry_source()).hexdigest()
    header = (_RUNTIME_PREAMBLE +
        f'CONFIG = {pprint.pformat({**binding, "component_revision": revision}, sort_dicts=True)}\n'
        f'PROMPTS = {pprint.pformat(prompts, sort_dicts=True)}\nNAMESPACE = {NAMESPACE!r}\n' +
        f'MODEL = {pprint.pformat(load_model(),sort_dicts=False)}\n' +
        ("ACTIVITY_PROGRAM = r'''\n" + program + "\n'''\n" if program else 'ACTIVITY_PROGRAM = ""\n') +
        ("CASE_PROGRAM = r'''\n" + case_program_text + "\n'''\n" if case_program_text else 'CASE_PROGRAM = ""\n') +
        f'CASE_MODES = frozenset({tuple(sorted(case_program.CASE_MODES))!r})\n' +
        f'CASE_CONFIRM_ACTIONS = {case_confirmation.CASE_CONFIRM_ACTIONS!r}\n' +
        f'CASE_REQUEST_FIELDS = frozenset({tuple(sorted(case_confirmation.CASE_REQUEST_FIELDS))!r})\n' +
        f'READ_OPERATIONS = frozenset({tuple(sorted(governance_graph.READ_OPERATIONS))!r})\n'
        f'PROJECTION_FIELDS = frozenset({tuple(sorted(consultation.PROJECTION_FIELDS))!r})\n'
        'logger = logging.getLogger("ai_data_governance")\n\n')
    source = (header + body + "\n").encode()
    compile(source, "governance_runtime.py", "exec")
    return source


def _governance_symbols(modules):
    definitions = {}
    for module in modules:
        for node in ast.parse(inspect.getsource(module)).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in definitions:
                    raise ValueError("Consultative assembly has a duplicate symbol")
                definitions[node.name] = node
    selected, pending = set(), ["DataGovernanceAgent"]
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        selected.add(name)
        pending.extend(n.id for n in ast.walk(definitions[name]) if isinstance(n, ast.Name)
            and n.id in definitions and n.id not in selected)
    return selected
