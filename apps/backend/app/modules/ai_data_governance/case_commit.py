"""Per-case durable admission and readback; no model tools or business mutations.

The required authorizer supplies an exact caller/session-bound confirmation.
Delta writes remain separate commits. A confirmed-state receipt is published
only after every row matches; later transitions must present that receipt.
"""

from datetime import datetime, timezone
import asyncio

from .activity_records import activity_agent
from .case_changes import case_digest, case_typed, case_change_receipt, validate_case_change, case_confirmation, case_effect_admission
from .inventory import canonical, identity
from .model_output import model_json
from .projection_query import consultation_scope_ids


class CasePending(RuntimeError):
    pass


def _same(left, right):
    def serial(value):
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
        if isinstance(value, list):
            return [serial(item) for item in value]
        if isinstance(value, dict):
            return {key: serial(item) for key, item in value.items()}
        return value
    return canonical(serial(left)) == canonical(serial(right))


class CaseCommitter:
    def __init__(self, repository, journal, binding, authorize, check_active, *, now):
        self.repository, self.journal, self.binding = repository, journal, binding
        self.authorize, self.check_active, self.now = authorize, check_active, now

    def commit(self, change):
        self.check_active()
        validate_case_change(change)
        grant = self.authorize(change)
        self._check_confirmation(change, grant)
        key = identity("case-operation", change["operation_id"])
        self.journal.create("outcomes", key, change)
        if not _same(self.journal.read("outcomes", key), change):
            raise ValueError("The frozen case operation changed")
        expected = case_change_receipt(change)
        receipt = self.journal.read("materialized", key)
        if receipt is not None:
            if not _same(receipt, expected):
                raise ValueError("The historical case receipt changed")
            # This confirms a historical registration, not its continued applicability.
            return receipt
        state_key = identity("case-state-confirmed", change["case"]["case_id"], case_digest(change["case"]))
        claim_key = identity("case-state-writer", change["case"]["case_id"], case_digest(change["previous"]))
        claim = {"operation_id": change["operation_id"], "input_digest": expected["input_digest"]}
        existing = self.journal.read("claims", claim_key)
        if existing is not None and not _same(existing, claim):
            raise CasePending("Another operation owns this case state")
        if existing is None:
            self._prepare_new(change, grant)
        admitted = self.journal.create("claims", claim_key, claim) if existing is None else False
        if admitted:
            try:
                self._prepare_new(change, grant)
                self.check_active()
                self._check_before(change)
                self._check_effect(change, grant)
            except (Exception, asyncio.CancelledError):
                # This acknowledged winner has not entered any Delta writer.
                self.journal.create("responses", key, self._abort(change))
                raise
            self._write(change, grant)
        if not self._matches(change):
            raise CasePending("Case write is partial or uncertain; reconcile without another write")
        self.check_active()
        self.journal.create("materialized", state_key, expected)
        self.journal.create("materialized", key, expected)
        return expected

    @staticmethod
    def _abort(change):
        return dict(kind="GOVERNANCE_CASE_CHANGE", operation_id=change["operation_id"],
            input_digest=identity("case-change", change), status="NOT_STARTED")

    def reconcile(self, change):
        """Seal exact completed rows without a fresh grant or another Delta write.

        The caller still authenticates the current user/session. Expiry prevents
        new decisions; it does not erase an already admitted historical result.
        """
        self.check_active()
        validate_case_change(change)
        if change["scope_id"] not in consultation_scope_ids(self.binding):
            raise PermissionError("Historical case escaped the installed coverage")
        key = identity("case-operation", change["operation_id"])
        expected = case_change_receipt(change)
        claim_key = identity("case-state-writer", change["case"]["case_id"], case_digest(change["previous"]))
        claim = dict(operation_id=change["operation_id"], input_digest=expected["input_digest"])
        if (not _same(self.journal.read("outcomes", key), change)
                or not _same(self.journal.read("claims", claim_key), claim)):
            raise CasePending("Reconciliation requires the original admitted case change")
        receipt = self.journal.read("materialized", key)
        if receipt is not None:
            if not _same(receipt, expected):
                raise ValueError("The historical case receipt changed")
            return receipt
        if not self._matches(change):
            raise CasePending("Case rows remain partial or uncertain; no additional writer is admitted")
        self.check_active()
        state_key = identity("case-state-confirmed", change["case"]["case_id"], case_digest(change["case"]))
        self.journal.create("materialized", state_key, expected)
        self.journal.create("materialized", key, expected)
        return expected

    def repair_not_started(self, change):
        """One explicit repair of a sealed pre-writer abort, never an uncertain write."""
        self.check_active()
        validate_case_change(change)
        grant = self.authorize(change)
        self._check_confirmation(change, grant)
        key = identity("case-operation", change["operation_id"])
        claim_key = identity("case-state-writer", change["case"]["case_id"], case_digest(change["previous"]))
        claim = dict(operation_id=change["operation_id"], input_digest=identity("case-change", change))
        if (not _same(self.journal.read("outcomes", key), change)
                or not _same(self.journal.read("claims", claim_key), claim)
                or not _same(self.journal.read("responses", key), self._abort(change))):
            raise CasePending("Repair requires the exact frozen operation and its sealed pre-writer abort")
        if self._matches(change):
            return self.commit(change)
        self._prepare_new(change, grant, allow_aborted_run=True)
        self._check_before(change)
        repair_key = identity("case-pre-writer-repair-once", key)
        if not self.journal.create("claims", repair_key, claim):
            raise CasePending("The single case repair is already admitted; reconcile its result")
        self._prepare_new(change, grant, allow_aborted_run=True)
        self.check_active()
        self._check_before(change)
        self._write(change, grant)
        return self.commit(change)

    def _prepare_new(self, change, grant, *, allow_aborted_run=False):
        self._check_effect(change, grant)
        self._check_references(change, allow_aborted_run=allow_aborted_run)
        if change["previous"] is not None:
            previous_key = identity("case-state-confirmed", change["case"]["case_id"], case_digest(change["previous"]))
            previous = self.journal.read("materialized", previous_key)
            if (previous is None or previous.get("state_digest") != case_digest(change["previous"])
                    or previous.get("assets_digest") != identity("case-assets", change["assets"])
                    or not _same(previous.get("decision_head"), change["previous_decision"])):
                raise CasePending("The prior complete case state is not confirmed")

    def _check_before(self, change):
        expected = [] if change["previous"] is None else [change["previous"]]
        if not _same(self.repository.read_by_keys("AI_DG_CASE", [{"case_id": change["case"]["case_id"]}]), expected):
            raise CasePending("The case changed before this admitted write")

    def _check_confirmation(self, change, grant):
        case_confirmation(change, grant, self.now())

    def _one(self, table, key):
        rows = self.repository.read_by_keys(table, [key])
        if len(rows) != 1:
            raise ValueError("A unique persisted case reference is required: " + table)
        return rows[0]

    def _check_references(self, change, *, allow_aborted_run=False):
        scope_id = change["scope_id"]
        if scope_id not in consultation_scope_ids(self.binding):
            raise PermissionError("Case escaped the configured installation")
        scope = self._one("AI_DG_SCOPE", {"scope_id": scope_id})
        if (scope["platform_key"] != self.binding["platform_id"] or scope["workspace_key"] != self.binding["workspace_key"]
                or scope["enabled"] is not True):
            raise PermissionError("Case scope is not enabled in this installation")
        self._check_activity_run(change, allow_aborted_run=allow_aborted_run)
        for link in change["assets"]:
            self._check_asset(change, link)
        if change["previous_decision"] is not None:
            decision = self._one("AI_DG_DECISION", {"decision_id": change["previous_decision"]["decision_id"]})
            if not _same(decision, change["previous_decision"]):
                raise ValueError("The prior decision changed")

    def _check_activity_run(self, change, *, allow_aborted_run=False):
        run = self._one("AI_DG_RUN", {"run_id": change["run_id"]})
        manifest = model_json(run["manifest_json"])
        producer = activity_agent(self.binding)
        registered = self._one("AI_DG_AGENT", {"agent_id": producer["agent_id"]})
        expected_producer = ({**producer, "deployment_version": registered["deployment_version"]}
            if self.binding["deployment_stage"] == "TEST" else producer)
        observed = datetime.fromisoformat(change["observed_at"])
        statuses = {"RUNNING", "SUCCEEDED"}
        if allow_aborted_run and _same(self.journal.read("responses", identity("case-operation", change["operation_id"])), self._abort(change)):
            statuses |= {"PARTIAL", "CANCELLED"}
        if (run["scope_id"] != change["scope_id"] or run["run_kind"] != "AGENT" or run["status"] not in statuses
                or run["agent_id"] != producer["agent_id"] or not _same(registered, expected_producer)
                or run["component_version"] != self.binding["component_revision"]
                or run["started_at"] > observed or run["finished_at"] is not None and run["finished_at"] < observed
                or manifest.get("actor_hash") != change["actor_ref"] or manifest.get("session_hash") != change["session_ref"]):
            raise PermissionError("Case operation has no matching authenticated activity run")

    def _check_asset(self, change, link):
        if link["impact_role"] != "SUBJECT":
            raise ValueError("A directional impact role requires an admitted relationship proof")
        asset = self._one("AI_DG_ASSET", {"asset_id": link["asset_id"]})
        projection = self._one("AI_DG_SCOPE_ASSET", {"scope_id": change["scope_id"], "asset_id": link["asset_id"]})
        version = self._one("AI_DG_ASSET_VERSION", {"version_id": link["assessed_version_id"]})
        historical = change["action"] == "REVOKE"
        if (asset["platform_key"] != self.binding["platform_id"] or version["asset_id"] != asset["asset_id"]
                or not historical and (projection["verified_version_id"] != version["version_id"]
                    or asset["current_version_id"] != version["version_id"] or asset["lifecycle_state"] != "ACTIVE"
                    or asset["identity_status"] == "AMBIGUOUS" or projection["consistency_state"] != "VERIFIED")):
            raise ValueError("Case asset and version are not a current verified pair")
        if link["evidence_id"] is None:
            raise ValueError("This case controller requires persisted supporting evidence")
        self._check_proof(change, link, historical=historical)

    def _check_proof(self, change, link, *, historical):
        proof = self._one("AI_DG_EVIDENCE", {"evidence_id": link["evidence_id"]})
        if (proof["scope_id"] != change["scope_id"] or proof["asset_version_id"] != link["assessed_version_id"]
                or not historical and (proof["coverage_state"] not in {"COMPLETE", "PARTIAL"}
                    or proof["observed_at"] > self.now()
                    or proof["expires_at"] is not None and proof["expires_at"] <= self.now())):
            raise ValueError("Case evidence is expired or belongs to another assessed version")

    def _check_effect(self, change, grant):
        self.check_active()
        case_effect_admission(change, grant, self.now())

    def _write(self, change, grant):
        for table, records in (("AI_DG_CASE_ASSET", change["assets"]), ("AI_DG_DECISION", change["decisions"])):
            self._check_effect(change, grant)
            self.repository.insert_immutable(table, case_typed(table, records))
        self._check_effect(change, grant)
        rows = case_typed("AI_DG_CASE", [change["case"]])
        if change["previous"] is None:
            self.repository.insert_immutable("AI_DG_CASE", rows)
        else:
            self.repository.update_current("AI_DG_CASE", rows, expected=case_typed("AI_DG_CASE", [change["previous"]]))

    def _matches(self, change):
        if not _same(self.repository.read_by_keys("AI_DG_CASE", [{"case_id": change["case"]["case_id"]}]), [change["case"]]):
            return False
        for table, records, fields in (("AI_DG_CASE_ASSET", change["assets"], ("case_id", "asset_id")),
                ("AI_DG_DECISION", change["decisions"], ("decision_id",))):
            actual = self.repository.read_by_keys(table, [{field: row[field] for field in fields} for row in records])
            order = lambda row: tuple(row[field] for field in fields)
            if not _same(sorted(actual, key=order), sorted(case_typed(table, records), key=order)):
                return False
        return True
