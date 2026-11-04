"""Central metadata reads with invocation-isolated, mandatory Oracle memory."""

from __future__ import annotations

from contextlib import ExitStack
import logging
import asyncio
import os
import re
from uuid import UUID

from .caller_transport import _caller_identity, native_central_transport
from .consultation import ConsultationHooks, SourceObservationExpired
from .central_sql import native_description_confirmation, native_descriptions_confirmation
from .governance_graph import build_governance_graph, governance_reference_cards
from .inventory import canonical, identity
from .model_output import model_json
from .projection_query import native_projection_query
from .response_graph import governance_visual_response
from .native_activity import NativeActivity
from .native_cases import NativeCaseActions
from .case_confirmation import case_confirmation_command
from .case_previews import render_case_preview, case_preview_text


CONFIG = {}
PROMPTS = {}
ACTIVITY_PROGRAM = ""
CASE_PROGRAM = ""
logger = logging.getLogger("ai_data_governance")


def is_consultation_throttling(error):
    seen = set()
    for _ in range(4):
        if error is None or id(error) in seen:
            return False
        seen.add(id(error))
        status = getattr(error, 'status', None)
        if type(status) is int:
            return status == 429
        if (type(error).__module__ == 'aidputils.agents.exceptions'
                and type(error).__name__ == 'AidpSystemException'
                and str(error).strip() == 'OCI GenAI: The model is rate-limited. Please retry shortly.'):
            return True
        error = error.__cause__ or error.__context__
    return False


class ConsultationModelRetry:
    """Retry rejected inference only; every attempt consumes the invocation budget."""
    def __init__(self):
        self.calls = 0

    async def invoke(self, call, check_identity, phase):
        from random import uniform

        for attempt in range(3):
            if self.calls >= 9:
                raise RuntimeError('Consultative model call budget reached')
            check_identity()
            self.calls += 1
            try:
                return await call()
            except Exception as error:
                if not is_consultation_throttling(error) or attempt == 2 or self.calls >= 9:
                    raise
                delay = 15 * (2 ** attempt) + uniform(0, 3)
                logger.warning('Governance model throttled: phase=%s attempt=%s backoff_seconds=%.1f',
                               phase, attempt + 1, delay)
                await asyncio.sleep(delay)


def consultation_config(native_config, binding, caller_context):
    _, subject, session = _caller_identity(caller_context)
    # Keep a UUID-shaped thread key while isolating native subjects and agents.
    digest = identity("governance-thread-v2", binding["platform_id"], binding["workspace_key"],
        binding["agent_key"], subject, session)
    thread = str(UUID(digest[:32]))
    metadata = native_config.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Invalid native invocation metadata")
    revision = binding.get("component_revision")
    if revision is not None:
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
            raise ValueError("Invalid served Governance revision")
        metadata = {**metadata, "component_revision": revision}
    return {**native_config, "metadata": metadata, "configurable": {"thread_id": thread, "checkpoint_ns": ""}, "recursion_limit": 10}


async def finish_consultation_activity(activity,state,*,failed=False,cancelled=False):
    """Keep the durable outcome; Spark monitoring is outside answer delivery."""
    if activity is None:
        return {}
    try:
        return {"activity_record":await activity.finish(state,failed=failed,cancelled=cancelled,materialize=False)}
    except Exception:
        logger.warning("Governance activity remains pending")
        return {"activity_pending":True}


async def invoke_consultation_graph(graph,question,config,activity):
    try:
        return await graph.ainvoke({"question":question},config=config)
    except asyncio.CancelledError:
        await finish_consultation_activity(activity,{},cancelled=True)
        raise


async def invoke_case_confirmation(cases, question, request_id, activity):
    try:
        return await cases.confirm(question, request_id=request_id)
    except asyncio.CancelledError:
        await finish_consultation_activity(activity, {}, cancelled=True)
        raise


class DataGovernanceAgent:
    def __init__(self):
        self.binding, self.prompts = CONFIG, PROMPTS
        self.memory = None

    def setup(self):
        from oracle_memory_clients.client import AsyncProxyCheckpointClient

        self.memory = AsyncProxyCheckpointClient(
            base_url=os.getenv("MEMORY_SERVER_URL") or os.getenv("MEMORY_URL") or "http://127.0.0.1:21100",
            agent="ai_data_governance")

    async def invoke(self, user_query, **kwargs):
        from aidputils.agents.toolkit.agent_helper import pre_invoke_setup, flow_setup
        from aidputils.agents.auth.util import auth_utils
        from langchain_core.messages import AIMessage

        config = pre_invoke_setup(**kwargs)
        caller = None
        activity = None
        activity_finished = False
        with ExitStack() as invocation:
            try:
                invocation.enter_context(flow_setup(**kwargs))
                config = consultation_config(config, self.binding, auth_utils.get_auth_context)
                if self.memory is None:
                    raise RuntimeError("Oracle Governance memory is unavailable")
                caller = native_central_transport(self.binding)
                if "storage_root" in self.binding:
                    import aidputils
                    activity=NativeActivity(self.binding,caller,ACTIVITY_PROGRAM,aidputils.secrets.get)
                    _,subject,session=_caller_identity(auth_utils.get_auth_context)
                    request_id=config['metadata'].get('governance_request_id')
                    await activity.begin(subject,session,user_query,request_id)
                async def query(sql, *, limit):
                    caller.check_identity()
                    return await native_projection_query(self.binding, sql, limit=limit, transport=caller)
                async def confirm_description(table_key, text_hash):
                    return await native_description_confirmation(self.binding, table_key, text_hash, transport=caller)
                async def confirm_descriptions(checks):
                    return await native_descriptions_confirmation(self.binding, checks, transport=caller)
                def hooks_factory():
                    return ConsultationHooks(self.binding, caller, query, code_usage_enabled=True,
                        confirm_description=confirm_description, confirm_descriptions=confirm_descriptions)
                hooks = hooks_factory()
                if case_confirmation_command(user_query) is not None:
                    cases = NativeCaseActions(self.binding, caller, activity, CASE_PROGRAM, query, hooks_factory,
                        get_context=auth_utils.get_auth_context)
                    confirmed = await invoke_case_confirmation(cases, user_query, activity.intent['request_id'], activity)
                    activity_result = await finish_consultation_activity(activity, {'response':confirmed})
                    activity_finished = True
                    return {"messages": [AIMessage(content=confirmed['answer_markdown'], additional_kwargs={
                        "governance":confirmed, "component_revision":self.binding['component_revision'], **activity_result})]}
                llm = None
                model_retry = ConsultationModelRetry()
                async def call_model(phase, payload, schema, run_config):
                    nonlocal llm
                    caller.check_identity()
                    if phase not in {"G1", "G3", "G4", "G5", "G6"}:
                        raise ValueError("Unsupported consultative model phase")
                    if llm is None:
                        from aidputils.agents.auth.signer.custom_remote_signer import CustomRemoteSigner
                        llm = consultation_model(self.binding, CustomRemoteSigner())
                    from langchain_core.messages import SystemMessage, HumanMessage
                    model_payload, aliases = consultation_model_sources(phase, payload)
                    messages = [SystemMessage(content=self.prompts["G0"] + "\n\n" + self.prompts[phase]),
                        HumanMessage(content=canonical({"input": model_payload, "json_schema": schema}))]
                    result = await model_retry.invoke(lambda: llm.ainvoke(messages, config=run_config),
                        caller.check_identity, phase)
                    return restore_consultation_sources(phase, result.content, aliases)
                async def prepare_case(state, run_config):
                    cases = NativeCaseActions(self.binding, caller, activity, CASE_PROGRAM, query, hooks_factory,
                        get_context=auth_utils.get_auth_context)
                    return await cases.preview(state, run_config)
                graph = build_governance_graph(authorize=hooks.authorize, resolve_assets=hooks.resolve_assets,
                    read=hooks._read_admitted, call_model=call_model, capabilities=hooks.capabilities, checkpointer=self.memory,
                    reauthorize_evidence=hooks._validate_admitted_evidence,
                    reobserve_evidence=hooks._reobserve_admitted_evidence,observer=activity.observe if activity else None,
                    prepare_case=prepare_case if activity else None)
                result = await invoke_consultation_graph(graph,user_query,config,activity)
                response = result["response"]
                content, relationships = governance_visual_response(result, render_governance_response(result), kwargs,
                    self.binding["component_revision"])
                activity_result=await finish_consultation_activity(activity,result)
                activity_finished=True
                return {"messages": [AIMessage(content=content,
                    additional_kwargs={"governance": response, "relationships": relationships,
                        "case_preview":result.get('case_preview',{}),
                        "component_revision": self.binding["component_revision"],**activity_result})]}
            except Exception as exc:
                if activity is not None and activity.intent is not None and activity.events and not activity_finished:
                    await finish_consultation_activity(activity,{},failed=True)
                logger.warning("Governance invocation failed: %s", type(exc).__name__)
                expired = isinstance(exc, SourceObservationExpired)
                denied = isinstance(exc, PermissionError) and not expired
                text = ("La evidencia de código venció durante la consulta. No se ha registrado ningún tratamiento; hace falta una nueva observación para continuar."
                    if expired else "No pude autorizar esta consulta con tu identidad y sesión actuales."
                    if denied else "No pude comprobar la evidencia necesaria para completar esta consulta. No se ha confirmado ningún resultado.")
                return {"messages": [AIMessage(content=text, additional_kwargs={"governance_error": True,
                    "error_kind": "evidence_expired" if expired else "authorization" if denied else "unavailable"})]}
            finally:
                if activity is not None:
                    activity.close()
                if caller is not None:
                    caller.session.close()


def render_governance_response(state):
    response = state["response"]
    body = response["answer_markdown"].strip()
    ids = response["cited_evidence_ids"]
    cards = {c["evidence_id"]: c for c in governance_reference_cards(state)}
    if len(ids) != len(set(ids)) or not set(ids).issubset(cards):
        raise ValueError("Answer source identifiers are duplicated or unavailable")
    limits = list(dict.fromkeys(text.strip() for text in response["limitations"] if text.strip()))
    extra = [case_preview_text(text) if not ids else text for text in limits if text.casefold() not in body.casefold()]
    if extra:
        body += "\n\n" + "\n".join("- " + text for text in extra)
    labels = {"CATALOG": "Catálogo", "STATIC_CODE": "Referencia estática", "RUNTIME_LINEAGE": "Linaje",
        "EXECUTION": "Ejecución", "QUALITY_ASSESSMENT": "Medición de calidad", "ACCESS_OBSERVATION": "Acceso observado",
        "CASE_RECORD": "Registro de caso"}
    sources = []
    for number, key in enumerate(ids, 1):
        card = cards[key]
        name = " ".join(card["asset_name"].split())
        ticks = "`" * (max((len(run) for run in re.findall(r"`+", name)), default=0) + 1)
        line = f"[{number}] {labels.get(card['kind'], 'Evidencia')}: {ticks} {name} {ticks}."
        if card["description_marked_ia"]:
            line += " Descripción marcada [IA]."
        sources.append(line)
    return body + ("\n\n" + "\n\n".join(sources) if sources else "") + render_case_preview(state.get('case_preview'))


def _map_source_ids(values, mapping):
    if not isinstance(values, list) or any(not isinstance(v, str) or v not in mapping for v in values):
        raise ValueError("Model citation is not an admitted source reference")
    return [mapping[value] for value in values]


def _map_review_sources(review, mapping):
    return {**review, "evidence_ids": _map_source_ids(review["evidence_ids"], mapping),
        "supported_claims": [{**claim, "evidence_ids": _map_source_ids(claim["evidence_ids"], mapping)}
            for claim in review["supported_claims"]]}


def _map_diagnosis_sources(diagnosis, mapping):
    return {**diagnosis, **{field:[{**claim,'evidence_ids':_map_source_ids(claim['evidence_ids'],mapping)}
        for claim in diagnosis[field]] for field in ('findings','potential_impacts')}}


def consultation_model_sources(phase, payload):
    if phase not in {"G3", "G4", "G5", "G6"}:
        return payload, {}
    field = "references" if phase == "G6" else "evidence"
    rows = payload[field]
    aliases = {"E" + str(index): row["evidence_id"] for index, row in enumerate(rows, 1)}
    forward = {value: key for key, value in aliases.items()}
    if len(forward) != len(rows):
        raise ValueError("Model evidence sources are duplicated")
    projected = {**payload, field: [{**row, "evidence_id": forward[row["evidence_id"]]} for row in rows]}
    if phase in {"G4", "G6"}:
        projected["review"] = _map_review_sources(payload["review"], forward)
    if payload.get('diagnosis'):
        projected['diagnosis'] = _map_diagnosis_sources(payload['diagnosis'], forward)
    if phase == 'G5':
        assets = payload['assets']
        aliases.update({'A'+str(i):r['asset_id'] for i,r in enumerate(assets,1)})
        aliases.update({'V'+str(i):r['version_id'] for i,r in enumerate(assets,1)})
        projected['assets'] = [{**r,'asset_id':'A'+str(i),'version_id':'V'+str(i)} for i,r in enumerate(assets,1)]
    return projected, aliases


def restore_consultation_sources(phase, content, aliases):
    if phase not in {"G3", "G4", "G5", "G6"}:
        return content
    value = model_json(content)
    if phase == "G3":
        value = _map_review_sources(value, aliases)
    elif phase == 'G4':
        value = _map_diagnosis_sources(value, aliases)
    elif phase == 'G5':
        value = {**value,'asset_ids':_map_source_ids(value['asset_ids'],{k:v for k,v in aliases.items() if k.startswith('A')}),
            'expected_versions':_map_source_ids(value['expected_versions'],{k:v for k,v in aliases.items() if k.startswith('V')})}
    else:
        value = {**value, "cited_evidence_ids": _map_source_ids(value["cited_evidence_ids"], aliases)}
    return canonical(value)


def consultation_model(binding, signer):
    from aidputils.agents.auth.client.generative_ai_inference_v2_client import GenerativeAiInferenceV2Client
    from aidputils.agents.guardrails.guarded_chat_oci_genai import GuardedChatOCIGenAI

    endpoint = f"https://inference.generativeai.{binding['region']}.oci.oraclecloud.com"
    return GuardedChatOCIGenAI(provider="generic", auth_type="REMOTE",
        client=GenerativeAiInferenceV2Client(endpoint=endpoint, signer=signer), service_endpoint=endpoint,
        compartment_id=binding["compartment_id"], model_id=binding["model_id"],
        model_kwargs={"temperature": 0, "max_tokens": 4096}, is_stream=True, guardrails_config={"policies": []})
