"""Native documentary entry: admitted workflow batches, Oracle memory and receipts.

This agent has no catalog-write, SQL, DDL or permission tools. Its model receives
only the frozen batch context. Publication/materialization are Python phases.
"""

from __future__ import annotations

import asyncio
import os
import re

from .documentary_graph import build_documentary_graph
from .documentary_hooks import JournalDocumentaryHooks
from .inventory import canonical, identity
from .journal import GovernanceJournal
from .service_transport import MetadataTransport, runtime_signer, runtime_object_storage
from .workflow_receipts import load_execution_receipt


CONFIG = {}
PROMPTS = {}


def admit_documentary_execution(request, binding: dict, batch: dict, *, expected_task_run_id: str | None = None) -> str:
    execution = batch.get("execution")
    if not isinstance(execution, dict) or set(execution) != {"job_key", "job_run_id", "scope_id", "inventory_run_id"}:
        raise ValueError("Documentary execution binding is unavailable")
    if execution["job_key"] != binding["job_key"] or not re.fullmatch(r"[a-f0-9-]{36}", execution["job_run_id"]):
        raise ValueError("Documentary batch belongs to a different workflow")
    for admission in batch["batch"]["admissions"]:
        if admission["scope_id"] != execution["scope_id"]:
            raise ValueError("Documentary work belongs to a different scope")
        if admission["model_ref"] != binding["model_id"] or admission["prompt_version"] != "DGD-PROMPTS-2":
            raise ValueError("The admitted model or prompt revision differs from the installed documentary agent")
    prefix = f"/workspaces/{binding['workspace_key']}"
    run, _ = request("GET", prefix + "/jobRuns/" + execution["job_run_id"])
    _active_documentary_run(run, execution)
    task_key = run.get("taskToTaskRunMap", {}).get("document_metadata")
    if not isinstance(task_key, str) or not re.fullmatch(r"[a-f0-9-]{36}", task_key):
        raise ValueError("The active documentary task is unavailable")
    if expected_task_run_id is not None and task_key != expected_task_run_id:
        raise ValueError("A replacement task attempt cannot adopt this documentary invocation")
    task, _ = request("GET", prefix + "/taskRuns/" + task_key)
    if not isinstance(task, dict) or any(task.get(key) != value for key, value in (
        ("key", task_key), ("jobKey", binding["job_key"]), ("jobRunKey", execution["job_run_id"]),
        ("taskKey", "document_metadata"))):
        raise ValueError("Documentary task identity differs from the admitted execution")
    _require_active_documentary_task(task)
    return task_key


def _active_documentary_run(run: dict, execution: dict) -> None:
    if not isinstance(run, dict) or run.get("key") != execution["job_run_id"] or run.get("jobKey") != execution["job_key"]:
        raise ValueError("Native execution differs from the documentary batch")
    _require_active_native_state(run.get("state"))


def _require_active_native_state(state: dict) -> None:
    if not isinstance(state, dict) or state.get("status") != "RUNNING" or state.get("isUserCanceledOrTimedOut"):
        raise ValueError("The admitted documentary execution is no longer active")


def _require_active_documentary_task(task: dict) -> None:
    state = task.get("state") or {}
    if ((task.get("task") or {}).get("type") != "AGENT_TASK" or task.get("endTime") is not None
            or state.get("isUserCanceledOrTimedOut")):
        raise ValueError("The admitted documentary task is not an active agent task")
    if state.get("status") == "RUNNING":
        return
    # Native Agent tasks remain PENDING during their synchronous A2A invocation.
    if (state.get("status") == "PENDING" and type(task.get("startTime")) is int
            and task["startTime"] > 0):
        return
    raise ValueError("The admitted documentary task has not started or is no longer active")


class DataGovernanceDocumentationAgent:
    def __init__(self):
        self.binding, self.prompts = CONFIG, PROMPTS
        self.signer = self.memory = self.llm = None

    def setup(self):
        import aidputils
        from oracle_memory_clients.client import AsyncProxyCheckpointClient

        self.signer = runtime_signer(self.binding, aidputils.secrets.get)
        self.memory = AsyncProxyCheckpointClient(
            base_url=os.getenv("MEMORY_SERVER_URL") or os.getenv("MEMORY_URL") or "http://127.0.0.1:21100",
            agent="ai_data_governance_documentation")

    async def invoke(self, user_query: str, **kwargs):
        from aidputils.agents.toolkit.agent_helper import pre_invoke_setup, flow_setup
        from langchain_core.messages import AIMessage
        import oci
        import requests

        config = pre_invoke_setup(**kwargs)
        if not isinstance(user_query, str) or not re.fullmatch(r"[a-f0-9-]{36}(?::[0-9]{1,3})?|[a-f0-9]{64}", user_query):
            raise ValueError("This internal agent requires the workflow's durable run or batch reference")
        if self.signer is None or self.memory is None:
            raise RuntimeError("Documentary agent setup is incomplete")
        with flow_setup(**kwargs) as config, requests.Session() as session:
            if not (config.get("configurable") or {}).get("thread_id"):
                raise ValueError("A native invocation session is required")
            config["recursion_limit"] = 12
            import aidputils
            journal = GovernanceJournal(runtime_object_storage(self.binding, self.signer, aidputils.secrets.get),
                platform_key=self.binding["platform_id"], workspace_key=self.binding["workspace_key"],
                storage_root=self.binding["storage_root"])
            request = MetadataTransport(self.binding, self.signer, session)
            batches = documentary_invocation_batches(journal, self.binding, user_query, request)
            summaries = []
            for batch_ref in batches:
                summaries.append(await self.process_batch(journal, request, batch_ref, config))
            return {"messages": [AIMessage(content=canonical({"phase": "document_metadata", "batches": summaries,
                "component_revision": self.binding["component_revision"], "catalog_writes": 0, "delta_writes": 0}))]}

    async def process_batch(self, journal, request, batch_ref, config):
        admitted_task_run = None
        async def authorize(batch, _config):
            nonlocal admitted_task_run
            admitted_task_run = await asyncio.to_thread(admit_documentary_execution, request, self.binding, batch,
                expected_task_run_id=admitted_task_run)
        hooks = JournalDocumentaryHooks(journal, call_model=self.call_model, authorize=authorize)
        graph = build_documentary_graph(load_batch=hooks.load_batch, claim_generation=hooks.claim_generation,
            generate=hooks.generate, review=hooks.review, save_outcomes=hooks.save_outcomes,
            check_active=hooks.check_active, checkpointer=self.memory)
        result = await graph.ainvoke({"batch_ref": batch_ref}, config=config)
        if result.get("completed") is not True:
            raise RuntimeError("Documentary outcomes were not durably confirmed")
        await hooks.check_active(config)
        outcome_receipt = await asyncio.to_thread(journal.read, "outcomes", batch_ref)
        if not isinstance(outcome_receipt, dict):
            raise RuntimeError("The durable documentary outcome receipt is unavailable")
        summary = {"schema_version": 1, "phase": "document_metadata", "batch_ref": batch_ref,
            "outcome_count": len(result["outcomes"]), "catalog_writes": 0, "delta_writes": 0,
            "component_revision": self.binding["component_revision"],
            "outcomes_digest": identity("documentary-outcomes", outcome_receipt),
            "states": sorted({item["status"] for item in result["outcomes"]})}
        return summary

    async def call_model(self, phase: str, payload: dict, schema: dict, config):
        from aidputils.agents.auth.client.generative_ai_inference_v2_client import GenerativeAiInferenceV2Client
        from aidputils.agents.guardrails.guarded_chat_oci_genai import GuardedChatOCIGenAI
        from langchain_core.messages import SystemMessage, HumanMessage

        if phase not in {"document", "review"}:
            raise ValueError("Invalid documentary model phase")
        if self.llm is None:
            endpoint = f"https://inference.generativeai.{self.binding['region']}.oci.oraclecloud.com"
            self.llm = GuardedChatOCIGenAI(provider="generic", auth_type="API_KEY",
                client=GenerativeAiInferenceV2Client(endpoint=endpoint, signer=self.signer),
                service_endpoint=endpoint, compartment_id=self.binding["compartment_id"],
                model_id=self.binding["model_id"], model_kwargs={"temperature": 0, "max_tokens": 4096},
                is_stream=True, guardrails_config={"policies": []})
        prompt = self.prompts["D0"] + "\n\n" + self.prompts["D1" if phase == "document" else "D2"]
        result = await self.llm.ainvoke([SystemMessage(content=prompt),
            HumanMessage(content=canonical({"input": payload, "json_schema": schema}))], config=config)
        return result.content


def documentary_invocation_batches(journal, binding, value, request):
    if re.fullmatch(r"[a-f0-9]{64}", value):
        return [value]
    run_id = value.split(":", 1)[0]
    manifest = load_execution_receipt(journal, binding, run_id)
    if manifest["documentary_revision"] != binding["component_revision"]:
        raise ValueError("The workflow requires a different documentary source revision")
    for row in manifest["scopes"]:
        batch = journal.read("batches", row["batch_ref"])
        admit_documentary_execution(request, binding, batch)
        if batch["execution"] != {"job_key": binding["job_key"], "job_run_id": run_id,
                "scope_id": row["scope_id"], "inventory_run_id": row["inventory_run_id"]}:
            raise ValueError("The staged batch belongs to another workflow run")
    return [row["batch_ref"] for row in manifest["scopes"]]
