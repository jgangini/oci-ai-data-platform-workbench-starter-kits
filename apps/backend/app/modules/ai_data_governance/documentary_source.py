"""Readable native agent assembly from the maintained graph, hooks and prompts."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path
import pprint
import re

from . import contracts, documentary_graph, documentary_hooks, documentation
from . import inventory, journal, model_output, native_documentary, service_transport
from . import workflow_receipts
from .prompts import documentary_prompts
from .runtime_source import _validate_runtime_bindings


def documentary_entry_source() -> bytes:
    return (Path(__file__).resolve().parent / 'aidp/workspaces/default/files/agents/documentation/documentation_agent.py').read_bytes()


def documentary_agent_source(binding: dict) -> bytes:
    if not isinstance(binding, dict) or set(binding) != {
        "region", "platform_id", "workspace_key", "credential_name", "identity_sha256", "storage_root",
        "job_key", "source_scopes", "model_id", "compartment_id"}:
        raise ValueError("Only explicit documentary installation bindings are accepted")
    _validate_runtime_bindings({k: v for k, v in binding.items() if k not in {"model_id", "compartment_id"}})
    for key, pattern in (("model_id", r"ocid1\.generativeaimodel\.oc1\.[a-z0-9.-]+"),
                         ("compartment_id", r"ocid1\.compartment\.oc1\.\.[a-z0-9]+")):
        if not isinstance(binding[key], str) or not re.fullmatch(pattern, binding[key]):
            raise ValueError("An existing model and compartment binding are required")
    prompts = documentary_prompts()
    body = "\n\n".join(inspect.getsource(symbol) for symbol in _documentary_symbols())
    revision = hashlib.sha256((body + inventory.canonical(prompts)).encode("utf-8") + documentary_entry_source()).hexdigest()
    config = {**binding, "component_revision": revision}
    header = (
        '"""AI_DG documentary agent. Maintained source: app/modules/ai_data_governance/."""\n'
        "\n"
        "import asyncio\nimport hashlib\nimport json\nimport os\nimport re\nfrom uuid import UUID\n"
        "from dataclasses import dataclass, asdict\nfrom typing import Annotated, Any, Literal, TypedDict\n"
        "from urllib.parse import unquote\n"
        "from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator\n\n"
        'Identifier = Annotated[str, Field(min_length=1, max_length=256)]\n'
        'ShortText = Annotated[str, Field(min_length=1, max_length=2000)]\n'
        'References = Annotated[list[Identifier], Field(max_length=128)]\n\n'
        f"CONFIG = {pprint.pformat(config, sort_dicts=True, width=100)}\n"
        f"PROMPTS = {pprint.pformat(prompts, sort_dicts=True, width=100)}\n\n"
    )
    source = (header + body + "\n").encode("utf-8")
    compile(source, "documentation_agent.py", "exec")
    return source


def _documentary_symbols() -> tuple:
    return (
        contracts.Contract, contracts.DocumentationItem, contracts.DocumentationEnvelope,
        contracts.DocumentationResult, contracts.DocumentReview, contracts.DocumentReviews,
        contracts.DocumentReviewEnvelope, documentation.DocumentationAdmission, documentation.AdmittedDraft,
        documentation._canonical, documentation._digest, documentation.documentation_generation_key,
        documentation._check_eligibility, documentation._admit_item, documentation.admit_documentation,
        documentation.admit_documentation_items, documentation.admit_document_reviews, documentation._known_reviews,
        inventory.canonical, inventory.identity, model_output._unique_members,
        model_output.model_json, model_output._invalid_constant,
        documentary_graph.DocumentaryState, documentary_graph._admission, documentary_graph._batch_snapshot,
        documentary_graph._batch_admissions, documentary_graph._batch_contexts, documentary_graph._initial_outcome,
        documentary_graph.DocumentaryNodes, documentary_graph._reviewed_outcomes, documentary_graph.build_documentary_graph,
        journal.JournalConflict, journal.GovernanceJournal, journal._validate_model_owner,
        journal.model_work_may_advance,
        journal._phase_precludes_model_work,
        documentary_hooks.JournalDocumentaryHooks, documentary_hooks._cached_model_response,
        documentary_hooks._response_generation_keys,
        documentary_hooks._reusable_generation,
        service_transport.runtime_signer, service_transport.runtime_sdk_config, service_transport.runtime_object_storage, service_transport.MetadataTransport,
        workflow_receipts.execution_receipt_key, workflow_receipts.load_execution_receipt,
        workflow_receipts._validate_receipt_scopes,
        native_documentary.documentary_invocation_batches,
        native_documentary.admit_documentary_execution, native_documentary._active_documentary_run,
        native_documentary._require_active_native_state, native_documentary._require_active_documentary_task,
        native_documentary.DataGovernanceDocumentationAgent,
    )
