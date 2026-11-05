"""Same-model monitoring for one confirmed case registration, in its own scope."""

import asyncio
from datetime import datetime

from .activity_records import activity_agent
from .case_changes import case_typed, case_time, case_change_receipt, validate_case_change
from .case_commit import CasePending, _same
from .inventory import canonical, identity
from .model_output import model_json
from .projection_query import consultation_scope_ids


def case_operation_run_id(binding, change):
    return identity("case-operation-run", binding["platform_id"], binding["workspace_key"],
        binding["agent_key"], change["operation_id"])


def case_run_start(binding, change):
    if change["scope_id"] not in consultation_scope_ids(binding) or change["run_id"] != case_operation_run_id(binding, change):
        raise ValueError("Case monitoring requires its own installed-scope run")
    producer = activity_agent(binding)
    manifest = {"kind": "GOVERNANCE_CASE_REGISTRATION", "operation_id": change["operation_id"],
        "actor_hash": change["actor_ref"], "session_hash": change["session_ref"],
        "input_digest": identity("case-change", change), "action": change["action"],
        "policy_version": change["policy_version"], "component_revision": binding["component_revision"],
        "deployment_stage": binding["deployment_stage"], "native_agent_key": binding["agent_key"],
        "execution_verified": False}
    run = {"run_id": change["run_id"], "scope_id": change["scope_id"], "parent_run_id": None,
        "agent_id": producer["agent_id"], "run_kind": "AGENT", "execution_key": change["operation_id"],
        "attempt_no": 1, "component_version": binding["component_revision"], "native_run_ref": None,
        "status": "RUNNING", "manifest_json": canonical(manifest), "started_at": change["observed_at"], "finished_at": None}
    event = case_run_event(change, "START", change["observed_at"], {"registration_verified": False})
    case_typed("AI_DG_RUN", [run])
    case_typed("AI_DG_EVENT", [event])
    return {"schema_version": 1, "kind": "GOVERNANCE_CASE_RUN", "run": run, "event": event}


def case_run_event(change, event_type, at, metrics):
    key = "case-registration:" + event_type
    return {"event_id": identity("case-run-event", change["run_id"], key), "run_id": change["run_id"],
        "event_key": key, "parent_event_id": None, "event_type": event_type, "asset_id": None,
        "occurred_at": at, "ingested_at": at, "summary": "Registro de caso: " + event_type,
        "metrics_json": canonical({"operation_id": change["operation_id"], "execution_verified": False, **metrics})}


class CaseActivityRun:
    def __init__(self, repository, journal, binding, controller, check_active, now):
        self.repository, self.journal, self.binding = repository, journal, binding
        self.controller, self.check_active, self.now = controller, check_active, now

    def _key(self, change, phase):
        return identity("case-run-" + phase, change["operation_id"])

    def _matches(self, name, row):
        key = "run_id" if name == "AI_DG_RUN" else "event_id"
        return _same(self.repository.read_by_keys(name, [{key: row[key]}]), case_typed(name, [row]))

    def _check_producer(self, change, *, historical):
        producer = activity_agent(self.binding)
        rows = self.repository.read_by_keys("AI_DG_AGENT", [{"agent_id": producer["agent_id"]}])
        if len(rows) == 1 and (historical or self.binding["deployment_stage"] == "TEST"):
            producer = {**producer, "deployment_version": rows[0]["deployment_version"]}
        if not _same(rows, [producer]):
            raise PermissionError("The case producer is not registered")
        scopes = self.repository.read_by_keys("AI_DG_SCOPE", [{"scope_id": change["scope_id"]}])
        expected = {"platform_key": self.binding["platform_id"], "workspace_key": self.binding["workspace_key"], "enabled": True}
        if len(scopes) != 1 or any(scopes[0].get(k) != v for k, v in expected.items()):
            raise PermissionError("The case scope is not enabled in this installation")

    def _start_record(self, change, *, historical):
        record = self.journal.read("executions", self._key(change, "start"))
        binding = self.binding
        if historical:
            if record is None:
                raise CasePending("No frozen case run exists")
            manifest = model_json(record["run"]["manifest_json"])
            binding = {**binding, "component_revision": record["run"]["component_version"],
                "deployment_stage": manifest["deployment_stage"]}
        expected = case_run_start(binding, change)
        if record is not None and not _same(record, expected):
            raise ValueError("The frozen case run changed")
        return expected

    def _start(self, change):
        self.check_active()
        self._check_producer(change, historical=False)
        record = self._start_record(change, historical=False)
        if datetime.fromisoformat(change["observed_at"]) > self.now():
            raise ValueError("Case registration cannot begin in the future")
        key = self._key(change, "start")
        self.journal.create("executions", key, record)
        claim = {"input_digest": identity("case-run-start", record)}
        if not self.journal.create("claims", key, claim):
            raise CasePending("This case run already started; reconcile without another writer")
        self.check_active()
        self.repository.insert_immutable("AI_DG_RUN", case_typed("AI_DG_RUN", [record["run"]]))
        self.repository.insert_immutable("AI_DG_EVENT", case_typed("AI_DG_EVENT", [record["event"]]))
        if not self._matches("AI_DG_RUN", record["run"]) or not self._matches("AI_DG_EVENT", record["event"]):
            raise CasePending("Case run start is incomplete; no case writer is admitted")

    def _terminal_record(self, change, start, status, receipt):
        key = self._key(change, "terminal")
        frozen = self.journal.read("executions", key)
        at = case_time(self.now()) if frozen is None else frozen["run"]["finished_at"]
        if datetime.fromisoformat(at) < datetime.fromisoformat(change["observed_at"]):
            raise ValueError("Case run cannot finish before its observation")
        selected_status = status if frozen is None else frozen["run"]["status"]
        if selected_status not in {"SUCCEEDED", "PARTIAL", "CANCELLED"}:
            raise ValueError("Invalid case registration result")
        verified = selected_status == "SUCCEEDED"
        if verified and not _same(receipt, case_change_receipt(change)):
            raise CasePending("Case run success requires the complete case receipt")
        result = {"run": {**start["run"], "status": selected_status, "finished_at": at},
            "event": case_run_event(change, "END", at, {"registration_verified": verified}),
            "case_receipt": case_change_receipt(change) if verified else None}
        if frozen is not None and not _same(frozen, result):
            raise ValueError("The frozen case run result changed")
        return result

    def _finish(self, change, status, receipt):
        self.check_active()
        start = self._start_record(change, historical=True)
        if not self._matches("AI_DG_EVENT", start["event"]):
            raise CasePending("The original case start event is not confirmed")
        final = self._terminal_record(change, start, status, receipt)
        key = self._key(change, "terminal")
        self.journal.create("executions", key, final)
        if self.journal.create("claims", key, {"input_digest": identity("case-run-terminal", final)}):
            self.repository.update_current("AI_DG_RUN", case_typed("AI_DG_RUN", [final["run"]]),
                expected=case_typed("AI_DG_RUN", [start["run"]]))
            self.repository.insert_immutable("AI_DG_EVENT", case_typed("AI_DG_EVENT", [final["event"]]))
        if not self._matches("AI_DG_RUN", final["run"]) or not self._matches("AI_DG_EVENT", final["event"]):
            raise CasePending("Case monitoring is partial; no repeated case write is admitted")
        return final

    def commit(self, change):
        validate_case_change(change)
        self.controller._check_confirmation(change, self.controller.authorize(change))
        self._start(change)
        try:
            receipt = self.controller.commit(change)
        except (Exception, asyncio.CancelledError) as error:
            try:
                self._finish(change, "CANCELLED" if isinstance(error, asyncio.CancelledError) else "PARTIAL", None)
            except Exception:
                pass  # Preserve the original failure; the frozen operation remains recoverable by readback.
            raise
        self._finish(change, "SUCCEEDED", receipt)
        return receipt

    def reconcile(self, change):
        self.check_active()
        self._check_producer(change, historical=True)
        self._start_record(change, historical=True)
        receipt = self.controller.reconcile(change)
        final = self._finish(change, "SUCCEEDED", receipt)
        if final["run"]["status"] != "SUCCEEDED":
            self._record_recovery(change, receipt)
        return receipt

    def repair_not_started(self, change):
        self.check_active()
        self._check_producer(change, historical=False)
        self._start_record(change, historical=False)
        receipt = self.controller.repair_not_started(change)
        final = self._finish(change, "SUCCEEDED", receipt)
        if final["run"]["status"] != "SUCCEEDED":
            self._record_recovery(change, receipt)
        return receipt

    def _record_recovery(self, change, receipt):
        key = self._key(change, "recovery")
        event = self.journal.read("executions", key)
        if event is None:
            event = case_run_event(change, "PROGRESS", case_time(self.now()),
                {"registration_verified": True, "case_receipt_digest": identity("case-receipt", receipt)})
            self.journal.create("executions", key, event)
        expected = case_run_event(change, "PROGRESS", event["occurred_at"],
            {"registration_verified": True, "case_receipt_digest": identity("case-receipt", receipt)})
        if (not _same(event, expected) or not datetime.fromisoformat(change["observed_at"])
                <= datetime.fromisoformat(event["occurred_at"]) <= self.now()):
            raise ValueError("The frozen case recovery event changed")
        if self.journal.create("claims", key, {"input_digest": identity("case-run-recovery", event)}):
            self.repository.insert_immutable("AI_DG_EVENT", case_typed("AI_DG_EVENT", [event]))
        if not self._matches("AI_DG_EVENT", event):
            raise CasePending("Historical recovery monitoring is not confirmed")
