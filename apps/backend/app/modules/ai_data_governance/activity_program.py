"""Fixed Spark operations for consultation monitoring, never assistant tools."""

from uuid import uuid4

from .activity_records import activity_agent, activity_typed_rows, validate_activity_batch
from .activity_commit import activity_receipt, activity_failure_receipt
from .inventory import canonical, identity
from .model_output import model_json
from .repository import DeltaRepository, checked_rows, _comparable
from .schema import NAMESPACE


def activity_scope(binding):
    scopes = [s for s in binding["source_scopes"] if s["schema_key"] == NAMESPACE]
    if len(scopes) != 1:
        raise ValueError("Shared activity requires the installed artifacts scope")
    scope = scopes[0]
    return {"scope_id":identity("AI_DG_SCOPE",binding["platform_id"],binding["workspace_key"],
        scope["catalog_key"],scope["schema_key"]),"platform_key":binding["platform_id"],
        "workspace_key":binding["workspace_key"],**scope}


def check_activity_scope(repository, binding):
    expected = activity_scope(binding)
    rows = repository.read_by_keys("AI_DG_SCOPE",[{"scope_id":expected["scope_id"]}])
    if len(rows) != 1 or any(rows[0][key] != value for key,value in expected.items()):
        raise ValueError("Activity scope is absent or belongs to another installation")


def register_activity_agent(repository, binding, expected_version):
    """Operator setup only, while this agent has no active writers."""
    agent = checked_rows("AI_DG_AGENT",[activity_agent(binding)])[0]
    previous = repository.read_by_keys("AI_DG_AGENT",[{"agent_id":agent["agent_id"]}])
    if not previous:
        if expected_version is not None:
            raise ValueError("The expected activity producer is absent")
        repository.insert_immutable("AI_DG_AGENT",[agent])
    elif canonical(previous) != canonical([agent]):
        if binding["deployment_stage"] != "PROD":
            raise ValueError("Development setup cannot replace an existing producer registry")
        before = previous[0]
        if (len(previous) != 1 or before["deployment_version"] != expected_version
                or canonical({**before,"deployment_version":agent["deployment_version"]}) != canonical(agent)):
            raise ValueError("The activity producer changed outside the admitted setup")
        repository.update_current("AI_DG_AGENT",[agent],expected=previous)
    if canonical(repository.read_by_keys("AI_DG_AGENT",[{"agent_id":agent["agent_id"]}])) != canonical([agent]):
        raise ValueError("Activity producer registration was not confirmed")
    return {"agent_id":agent["agent_id"],"deployment_version":agent["deployment_version"],"verified":True}


def prepare_activity_rows(repository, binding, batch, *, historical=False):
    validate_activity_batch(batch)
    if (canonical(batch["agent"]) != canonical(activity_agent(binding))
            or batch["scope_id"] != activity_scope(binding)["scope_id"]
            or model_json(batch["run"]["manifest_json"])["deployment_stage"] != binding["deployment_stage"]):
        raise ValueError("Activity escaped its installed producer or scope")
    registered = repository.read_by_keys("AI_DG_AGENT",[{"agent_id":batch["agent"]["agent_id"]}])
    if len(registered) != 1:
        raise ValueError("The activity producer is absent or ambiguous")
    expected = batch["agent"]
    if historical or binding["deployment_stage"] == "TEST":
        # DEV retains the logical/native agent identity; its served revision belongs to RUN.
        expected = {**expected,"deployment_version":registered[0]["deployment_version"]}
    if canonical(registered) != canonical([expected]):
        raise ValueError("The exact activity producer revision is not registered")
    return activity_typed_rows(batch)


def activity_rows_match(repository, rows):
    for name,records in rows.items():
        key = "run_id" if name == "AI_DG_RUN" else "event_id"
        actual = repository.read_by_keys(name,[{key:row[key]} for row in records])
        comparable = lambda values: sorted((_comparable(row) for row in values),key=lambda row:row[key])
        if canonical(comparable(actual)) != canonical(comparable(records)):
            return False
    return True


def activity_jobs_terminal(context, group):
    """Missing tracker information is uncertainty, not proof that work stopped."""
    try:
        tracker = context.statusTracker()
        jobs = tracker.getJobIdsForGroup(group)
        if not jobs:
            return False
        for key in jobs:
            info = tracker.getJobInfo(key)
            if info is None or info.status not in {"SUCCEEDED","FAILED"}:
                return False
        return True
    except Exception:
        return False


def run_activity_program(spark, binding, mode, payload, expected_version=None):
    if mode not in {"REGISTER_AGENT","APPEND_ACTIVITY","VERIFY_ACTIVITY"}:
        raise ValueError("Unknown fixed activity operation")
    spark.conf.set("spark.sql.session.timeZone","UTC")
    repository = DeltaRepository(spark,binding["storage_root"],max_batch_rows=32)
    check_activity_scope(repository,binding)
    if mode == "REGISTER_AGENT":
        if payload is not None:
            raise ValueError("Producer registration accepts no activity payload")
        return register_activity_agent(repository,binding,expected_version)
    if expected_version is not None:
        raise ValueError("Activity cannot change its registered producer")
    rows = prepare_activity_rows(repository,binding,payload,historical=mode == "VERIFY_ACTIVITY")
    if mode == "VERIFY_ACTIVITY":
        return activity_receipt(payload) if activity_rows_match(repository,rows) else {"verified":False}
    context = spark.sparkContext
    group = "ai_dg_activity_" + uuid4().hex
    previous = context.getLocalProperty("spark.jobGroup.id")
    context.setLocalProperty("spark.jobGroup.id",group)
    try:
        try:
            repository.insert_immutable("AI_DG_RUN",rows["AI_DG_RUN"])
            repository.insert_immutable("AI_DG_EVENT",rows["AI_DG_EVENT"])
            if not activity_rows_match(repository,rows):
                raise RuntimeError("Activity rows were not confirmed")
            return activity_receipt(payload)
        except Exception:
            if activity_jobs_terminal(context,group):
                return activity_failure_receipt(payload)
            raise RuntimeError("Activity execution is uncertain; no write repair admitted") from None
    finally:
        context.setLocalProperty("spark.jobGroup.id",previous)
