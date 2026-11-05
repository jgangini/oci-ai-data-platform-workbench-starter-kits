"""Shared Governance bindings, activity journals and case fixtures."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from app.modules.ai_data_governance.activity_records import activity_intent, activity_event, activity_batch, activity_typed_rows
from app.modules.ai_data_governance.activity_commit import activity_receipt
from app.modules.ai_data_governance.case_commit import CaseCommitter
from app.modules.ai_data_governance.inventory import identity
from app.modules.ai_data_governance.journal import JournalConflict


def binding():
    return {"region": "us-chicago-1", "platform_id": "ocid1.aidataplatform.oc1.us-chicago-1.test",
        "workspace_key": "11111111-1111-4111-8111-111111111111", "agent_key": "22222222-2222-4222-8222-222222222222",
        "query_compute_key": "33333333-3333-4333-8333-333333333333",
        "model_id": "ocid1.generativeaimodel.oc1.us-chicago-1.test", "compartment_id": "ocid1.compartment.oc1..test",
        "source_scopes": [{"catalog_key": "catalog", "schema_key": "catalog.schema"}],
        "credential_name": "existing-governance-credential", "identity_sha256": "a" * 64}


AT = datetime(2026, 11, 1, tzinfo=timezone.utc)


def activity_fixture():
    config={**binding(),'deployment_stage':'TEST','component_revision':'d'*64,
        'agent_code_path':'/Workspace/.artifact/ai_data_governance/agents/test/governance_agent.py',
        'source_scopes':[{'catalog_key':'oci_medallion','schema_key':'oci_medallion.oci_artifacts'}]}
    start=datetime(2026,11,1,tzinfo=timezone.utc)
    intent=activity_intent(config,subject='subject-a',session='session-a',
        request_id='44444444-4444-4444-8444-444444444444',question='Private question text',started_at=start)
    events=[activity_event('admission','completed',start+timedelta(microseconds=1)),
        activity_event('retrieve','completed',start+timedelta(seconds=1),asset_count=1,evidence_count=1)]
    return config,intent,activity_batch(intent,events,finished_at=start+timedelta(seconds=2),status='SUCCEEDED',result_state='answered')


class MemoryJournal:
    def __init__(self): self.values={}; self.create_calls=[]
    def read(self,kind,key): return deepcopy(self.values.get((kind,key)))
    def create(self,kind,key,value):
        self.create_calls.append((kind,key))
        if (kind,key) in self.values:
            if self.values[kind,key] != value: raise JournalConflict('Different frozen value')
            return False
        self.values[kind,key]=deepcopy(value)
        return True


class FixedRunner:
    def __init__(self): self.append_calls=0; self.verify_calls=0; self.persisted=None; self.lose_ack=False; self.partial=False
    def __call__(self,mode,batch):
        if mode=='APPEND_ACTIVITY':
            self.append_calls+=1
            self.persisted=deepcopy(batch) if not self.partial else None
            if self.lose_ack: raise TimeoutError('lost acknowledgement')
        else:
            assert mode=='VERIFY_ACTIVITY'; self.verify_calls+=1
        return activity_receipt(batch) if self.persisted==batch else {'verified':False}


class CaseJournal:
    def __init__(self): self.rows = {}
    def read(self, kind, key): return deepcopy(self.rows.get((kind, key)))
    def create(self, kind, key, row):
        if (kind, key) in self.rows:
            if self.rows[kind, key] != row: raise JournalConflict("Frozen row changed")
            return False
        self.rows[kind, key] = deepcopy(row)
        return True


class CaseRepository:
    def __init__(self): self.rows = {}; self.writes = []; self.after_case = None
    def read_by_keys(self, table, keys):
        return [deepcopy(row) for row in self.rows.get(table, [])
            if any(all(row[name] == value for name, value in key.items()) for key in keys)]
    def insert_immutable(self, table, rows):
        self.writes.append(table)
        for row in rows:
            if row not in self.rows.setdefault(table, []): self.rows[table].append(deepcopy(row))
        if table == "AI_DG_CASE" and self.after_case: self.after_case()
    def update_current(self, table, rows, *, expected):
        assert table in {"AI_DG_CASE", "AI_DG_RUN"} and self.rows[table] == expected
        self.writes.append(table); self.rows[table] = deepcopy(rows)
        if table == "AI_DG_CASE" and self.after_case: self.after_case()


def fixture():
    config, intent, activity = activity_fixture()
    repo, journal = CaseRepository(), CaseJournal()
    scope = intent["scope_id"]
    repo.rows = {
        "AI_DG_SCOPE": [dict(scope_id=scope, platform_key=config["platform_id"], workspace_key=config["workspace_key"], enabled=True)],
        "AI_DG_AGENT": [activity["agent"]],
        "AI_DG_RUN": [{**activity_typed_rows(activity)["AI_DG_RUN"][0], "status": "RUNNING", "finished_at": None}],
        "AI_DG_ASSET": [dict(asset_id="asset", platform_key=config["platform_id"], current_version_id="v1",
            lifecycle_state="ACTIVE", identity_status="RESOLVED")],
        "AI_DG_SCOPE_ASSET": [dict(scope_id=scope, asset_id="asset", verified_version_id="v1", consistency_state="VERIFIED")],
        "AI_DG_ASSET_VERSION": [dict(version_id="v1", asset_id="asset")],
        "AI_DG_EVIDENCE": [dict(evidence_id="proof", scope_id=scope, asset_version_id="v1", expires_at=None,
            coverage_state="COMPLETE", observed_at=AT)],
    }
    args = dict(subject="subject-a", session="session-a", request_id="77777777-7777-4777-8777-777777777777",
        scope_id=scope, run_id=activity["run"]["run_id"], policy_version="p1", at=AT + timedelta(seconds=3),
        assets=[dict(asset_id="asset", assessed_version_id="v1", impact_role="SUBJECT", evidence_id="proof")])
    def authorize(change):
        return dict(confirmed=True, actor_ref=change["actor_ref"], session_ref=change["session_ref"],
            input_digest=identity("case-change", change), policy_version="p1", expires_at=(AT + timedelta(hours=1)).isoformat())
    controller = CaseCommitter(repo, journal, config, authorize, lambda: None, now=lambda: AT + timedelta(seconds=10))
    return args, repo, journal, controller
