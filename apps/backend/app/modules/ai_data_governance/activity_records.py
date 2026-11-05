"""Typed, bounded consultation activity; no prompt text or model-authored writes."""

from datetime import datetime, timezone
import re
from uuid import UUID

from .inventory import canonical, identity
from .projection_query import consultation_scope_ids
from .schema import NAMESPACE
from .model_output import model_json
from .repository import checked_rows
from .schema import load_model


def activity_agent(binding):
    stage = binding.get("deployment_stage")
    if stage not in {"TEST", "PROD"}:
        raise ValueError("Activity requires an explicit deployment stage")
    key = str(UUID(binding["agent_key"]))
    if key != binding["agent_key"]:
        raise ValueError("Activity requires the bound native agent identity")
    path = binding.get("agent_code_path")
    if not isinstance(path,str) or not re.fullmatch(
            r"/Workspace/\.artifact/ai_data_governance/agents/[A-Za-z0-9_/-]+/governance_agent\.py",path):
        raise ValueError("Activity requires the installed Governance code path")
    revision = binding.get("component_revision")
    if not isinstance(revision,str) or not re.fullmatch(r"[0-9a-f]{64}",revision):
        raise ValueError("Activity requires the served component revision")
    role = "GOVERNANCE_ASSISTANT"
    return {"agent_id":identity("AI_DG_AGENT",binding["platform_id"],binding["workspace_key"],role),
        "platform_key":binding["platform_id"], "workspace_key":binding["workspace_key"], "agent_role":role,
        "native_agent_key":key, "deployment_version":revision, "code_path":path,
        "capabilities_json":canonical(["consult_metadata","read_case"]), "enabled":True}


def activity_time(value):
    if not isinstance(value,datetime) or value.utcoffset() is None:
        raise ValueError("Activity requires a timezone-aware observation time")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def activity_intent(binding, *, subject, session, request_id, question, started_at):
    if any(not isinstance(v,str) or not v or len(v)>4096 for v in (subject,session,question)):
        raise ValueError("Activity requires a bound conversation and question")
    if not isinstance(request_id,str) or str(UUID(request_id)) != request_id:
        raise ValueError("Activity requires a canonical per-request UUID")
    # The monitoring scope is the installation's artifacts scope, not a user ACL.
    selected = [s for s in binding["source_scopes"] if s["schema_key"] == NAMESPACE]
    if len(selected) != 1:
        raise ValueError("The installed artifacts scope is required for shared activity")
    scope_id = identity("AI_DG_SCOPE",binding["platform_id"],binding["workspace_key"],
        selected[0]["catalog_key"],selected[0]["schema_key"])
    if scope_id not in consultation_scope_ids(binding):
        raise ValueError("Activity escaped its configured scope")
    agent = activity_agent(binding)
    actor_hash,session_hash = identity("activity-subject",subject),identity("activity-session",session)
    operation = identity("consultation-activity",binding["platform_id"],binding["workspace_key"],
        binding["agent_key"],actor_hash,session_hash,request_id)
    return {"schema_version":1,"kind":"CONSULTATION_ACTIVITY","operation_id":operation,
        "scope_id":scope_id,"agent":agent,"actor_hash":actor_hash,"session_hash":session_hash,
        "question_hash":identity("activity-question",question),"request_id":request_id,"deployment_stage":binding["deployment_stage"],
        "started_at":activity_time(started_at)}


def activity_event(phase, transition, observed_at, *, asset_count=0, evidence_count=0):
    if phase not in {"admission","understand","retrieve","assess","respond","reobserve","invocation"}:
        raise ValueError("Unknown consultation phase")
    if transition not in {"started","completed","failed"}:
        raise ValueError("Unknown activity transition")
    if type(asset_count) is not int or not 0 <= asset_count <= 32:
        raise ValueError("Invalid activity asset count")
    if type(evidence_count) is not int or not 0 <= evidence_count <= 128:
        raise ValueError("Invalid activity evidence count")
    return {"phase":phase,"transition":transition,"occurred_at":activity_time(observed_at),
        "asset_count":asset_count,"evidence_count":evidence_count}


def activity_event_type(event):
    if event["transition"] == "failed":
        return "ERROR"
    if event["phase"] == "invocation":
        return "START" if event["transition"] == "started" else "END"
    return "PROGRESS"


def activity_batch(intent, events, *, finished_at, status, result_state):
    if status not in {"SUCCEEDED","PARTIAL","FAILED","CANCELLED","SKIPPED"} or result_state not in {
            "answered","partial","clarification","denied","not_evaluable","unavailable"}:
        raise ValueError("Unknown consultation result")
    if not isinstance(events,list) or not 1 <= len(events) <= 32:
        raise ValueError("Activity exceeds its event budget")
    end = activity_time(finished_at)
    start = activity_time(datetime.fromisoformat(intent["started_at"]))
    if start != intent["started_at"] or datetime.fromisoformat(end) < datetime.fromisoformat(start):
        raise ValueError("Activity ended before it started")
    run_id = identity("consultation-run",intent["operation_id"])
    if intent["deployment_stage"] not in {"TEST","PROD"}:
        raise ValueError("Activity requires its actual execution stage")
    manifest = {key:intent[key] for key in ("operation_id","actor_hash","session_hash","question_hash","request_id","deployment_stage")}
    manifest.update(schema_version=1,result_state=result_state,native_agent_key=intent["agent"]["native_agent_key"],
        component_revision=intent["agent"]["deployment_version"])
    run = {"run_id":run_id,"scope_id":intent["scope_id"],"parent_run_id":None,"agent_id":intent["agent"]["agent_id"],
        "run_kind":"AGENT","execution_key":intent["operation_id"],"attempt_no":1,
        "component_version":intent["agent"]["deployment_version"],"native_run_ref":None,"status":status,
        "manifest_json":canonical(manifest),"started_at":intent["started_at"],"finished_at":end}
    recorded=[]
    for index,event in enumerate(events):
        checked = activity_event(event["phase"],event["transition"],datetime.fromisoformat(event["occurred_at"]),
            asset_count=event["asset_count"],evidence_count=event["evidence_count"])
        if (event != checked or not datetime.fromisoformat(start) <= datetime.fromisoformat(event["occurred_at"])
                <= datetime.fromisoformat(end)):
            raise ValueError("Activity observation is outside its invocation")
        key = f"{index:02d}:{event['phase']}:{event['transition']}"
        recorded.append({"event_id":identity("consultation-event",run_id,key),"run_id":run_id,"event_key":key,
            "parent_event_id":None,"event_type":activity_event_type(event),"asset_id":None,
            "occurred_at":event["occurred_at"],"ingested_at":end,
            "summary":"Consulta de gobierno: "+event["phase"]+" / "+event["transition"],
            "metrics_json":canonical({"phase":event["phase"],"transition":event["transition"],"asset_count":event["asset_count"],"evidence_count":event["evidence_count"]})})
    return {"schema_version":1,"kind":"CONSULTATION_ACTIVITY","operation_id":intent["operation_id"],
        "scope_id":intent["scope_id"],"agent":intent["agent"],"run":run,"events":recorded}


def validate_activity_batch(batch):
    if (not isinstance(batch,dict) or set(batch) != {"schema_version","kind","operation_id","scope_id","agent","run","events"}
            or type(batch["schema_version"]) is not int or batch["schema_version"] != 1
            or batch["kind"] != "CONSULTATION_ACTIVITY" or len(canonical(batch).encode()) > 131072):
        raise ValueError("Invalid bounded activity batch")
    try:
        return checked_activity_content(batch)
    except (KeyError,TypeError,AttributeError):
        raise ValueError("Activity batch has invalid field shapes") from None


def checked_activity_manifest(batch):
    """Validate invocation identity independently from its projected Delta rows."""
    agent,run = batch["agent"],batch["run"]
    manifest = model_json(run["manifest_json"],max_bytes=8192)
    for key in ("scope_id","operation_id"):
        if not isinstance(batch[key],str) or not re.fullmatch(r"[0-9a-f]{64}",batch[key]):
            raise ValueError("Activity has an invalid scope or operation")
    for key in ("actor_hash","session_hash","question_hash"):
        if not isinstance(manifest[key],str) or not re.fullmatch(r"[0-9a-f]{64}",manifest[key]):
            raise ValueError("Activity must retain digests instead of conversation text")
    request_id = manifest["request_id"]
    if not isinstance(request_id,str) or str(UUID(request_id)) != request_id:
        raise ValueError("Activity has an invalid request reference")
    operation = identity("consultation-activity",agent["platform_key"],agent["workspace_key"],agent["native_agent_key"],
        manifest["actor_hash"],manifest["session_hash"],request_id)
    if operation != batch["operation_id"]:
        raise ValueError("Activity belongs to another invocation")
    return manifest


def checked_activity_content(batch):
    agent,run = batch["agent"],batch["run"]
    manifest = checked_activity_manifest(batch)
    expected_agent = activity_agent({"platform_id":agent["platform_key"],"workspace_key":agent["workspace_key"],
        "agent_key":agent["native_agent_key"],"component_revision":agent["deployment_version"],
        "deployment_stage":manifest["deployment_stage"],"agent_code_path":agent["code_path"]})
    if canonical(expected_agent) != canonical(agent) or type(run["attempt_no"]) is not int or run["attempt_no"] != 1:
        raise ValueError("Activity has an invalid producer or attempt")
    intent = {"operation_id":batch["operation_id"],"scope_id":batch["scope_id"],"agent":agent,"started_at":run["started_at"],
        **{key:manifest[key] for key in ("actor_hash","session_hash","question_hash","request_id","deployment_stage")}}
    events=[]
    for event in batch["events"]:
        metrics = model_json(event["metrics_json"],max_bytes=1024)
        events.append(activity_event(metrics["phase"],metrics["transition"],
            datetime.fromisoformat(event["occurred_at"]),asset_count=metrics["asset_count"],evidence_count=metrics["evidence_count"]))
    expected = activity_batch(intent,events,finished_at=datetime.fromisoformat(run["finished_at"]),
        status=run["status"],result_state=manifest["result_state"])
    if canonical(batch) != canonical(expected):
        raise ValueError("Activity rows differ from their fixed projection")
    return batch


def activity_typed_rows(batch):
    validate_activity_batch(batch)
    model = {table["name"]:table for table in load_model()["tables"]}
    result = {}
    for name,rows in (("AI_DG_RUN",[batch["run"]]),("AI_DG_EVENT",batch["events"])):
        timestamps = {c["name"] for c in model[name]["columns"] if c["type"] == "TIMESTAMP"}
        converted = [{key:datetime.fromisoformat(value) if key in timestamps and value is not None else value
            for key,value in row.items()} for row in rows]
        result[name] = checked_rows(name,converted,limit=32)
    return result
