"""Fresh, bounded CASE state and persisted catalog-evidence readmission."""

from datetime import datetime
import re

from .case_changes import case_record_snapshot, case_versions
from .case_commit import _same
from .case_confirmation import CASE_REQUEST_FIELDS
from .case_reader import case_history_query, case_history_groups, checked_case_reference
from .inventory import canonical, identity
from .model_output import model_json
from .projection_query import (_bounded_statement, _control_table, _query_text, consultation_scope_ids,
    metadata_json_options, projection_search)


def case_specification_links(binding, spec):
    if (not isinstance(spec, dict) or set(spec) != CASE_REQUEST_FIELDS or len(canonical(spec).encode()) > 131072
            or spec["scope_id"] not in consultation_scope_ids(binding)
            or spec["action"] not in {"OPEN", "PROPOSE", "APPROVE", "REJECT", "REVOKE"}):
        raise ValueError("Invalid case readmission specification")
    assets = spec["assets"]
    if not isinstance(assets, list) or not 1 <= len(assets) <= 16:
        raise ValueError("Case readmission requires one bounded assessed set")
    links = [checked_case_assessed_link(row, spec["previous"]) for row in assets]
    if len({row["asset_id"] for row in links}) != len(links):
        raise ValueError("Case assessed assets are duplicated")
    return sorted(links, key=lambda row: row["asset_id"])


def checked_case_assessed_link(row, previous):
    required = {"asset_id", "assessed_version_id", "impact_role", "evidence_id"}
    if not isinstance(row, dict) or set(row) not in (required, required | {"case_id"}):
        raise ValueError("Invalid assessed case link")
    if (not isinstance(row["asset_id"], str) or not 1 <= len(row["asset_id"]) <= 256 or row["impact_role"] != "SUBJECT"
            or any(not isinstance(row[key], str) or not re.fullmatch(r"[0-9a-f]{64}", row[key])
                for key in ("assessed_version_id", "evidence_id"))):
        raise ValueError("Case links require exact persisted versions and evidence")
    if "case_id" in row and (previous is None or row["case_id"] != previous["case_id"]):
        raise ValueError("Case link belongs to another case")
    return {key: row[key] for key in required}


def case_catalog_evidence_query(binding, spec):
    links = case_specification_links(binding, spec)
    fields = ("evidence_id", "scope_id", "asset_version_id", "producer_run_id", "evidence_kind", "source_ref",
        "evidence_hash", "facts_json", "coverage_state", "observed_at", "expires_at")
    evidence = ",".join(f"'{key}',e.{key}" for key in fields)
    fields = {"scope_id": "e.scope_id", "asset_id": "a.asset_id", "asset_kind": "a.asset_kind",
        "version_id": "v.version_id", "metadata_hash": "v.metadata_hash", "metadata_json": "v.metadata_json",
        "qualified_name": "v.qualified_name", "current_version_id": "a.current_version_id",
        "verified_version_id": "p.verified_version_id", "identity_status": "a.identity_status",
        "lifecycle_state": "a.lifecycle_state", "consistency_state": "p.consistency_state"}
    payload = ",".join(f"'{key}',{value}" for key, value in fields.items())
    conditions = " OR ".join(f"(a.asset_id={_query_text(link['asset_id'])} AND v.version_id={_query_text(link['assessed_version_id'])} "
        f"AND e.evidence_id={_query_text(link['evidence_id'])})" for link in links)
    sql = (f"SELECT to_json(named_struct({payload},'evidence',named_struct({evidence})),{metadata_json_options()}) AS payload "
        f"FROM {_control_table('AI_DG_EVIDENCE')} e JOIN {_control_table('AI_DG_ASSET_VERSION')} v ON v.version_id=e.asset_version_id "
        f"JOIN {_control_table('AI_DG_ASSET')} a ON a.asset_id=v.asset_id "
        f"JOIN {_control_table('AI_DG_SCOPE_ASSET')} p ON p.asset_id=a.asset_id AND p.scope_id=e.scope_id "
        f"JOIN {_control_table('AI_DG_SCOPE')} s ON s.scope_id=e.scope_id "
        f"WHERE e.scope_id={_query_text(spec['scope_id'])} AND a.platform_key={_query_text(binding['platform_id'])} "
        f"AND s.platform_key={_query_text(binding['platform_id'])} AND s.workspace_key={_query_text(binding['workspace_key'])} "
        f"AND s.enabled=TRUE AND ({conditions}) ORDER BY a.asset_id,e.evidence_id LIMIT 17")
    return _bounded_statement(sql)


def checked_case_catalog_proof(binding, spec, link, row, *, now):
    fields = {"scope_id", "asset_id", "asset_kind", "version_id", "metadata_hash", "metadata_json", "qualified_name",
        "current_version_id", "verified_version_id", "identity_status", "lifecycle_state", "consistency_state", "evidence"}
    if not isinstance(row, dict) or set(row) != fields:
        raise ValueError("Invalid persisted case evidence projection")
    if row["scope_id"] != spec["scope_id"] or row["asset_id"] != link["asset_id"] or row["version_id"] != link["assessed_version_id"]:
        raise PermissionError("Case evidence belongs to another assessed pair")
    facts = model_json(row["metadata_json"])
    metadata_hash = identity("metadata", facts)
    if metadata_hash != row["metadata_hash"] or identity("AI_DG_ASSET_VERSION", row["asset_id"], metadata_hash) != row["version_id"]:
        raise ValueError("The historical case metadata hash changed")
    checked_case_evidence_identity(binding, spec, link, row, facts)
    checked_case_evidence_time(row["evidence"], historical=spec["action"] == "REVOKE", now=now)
    if spec["action"] != "REVOKE" and (row["current_version_id"] != row["version_id"] or row["verified_version_id"] != row["version_id"]
            or row["identity_status"] not in {"RESOLVED", "FALLBACK"} or row["lifecycle_state"] != "ACTIVE" or row["consistency_state"] != "VERIFIED"):
        raise PermissionError("Case assessment is no longer the current verified version")


def checked_case_evidence_identity(binding, spec, link, row, facts):
    proof = row["evidence"]
    fields = {"evidence_id", "scope_id", "asset_version_id", "producer_run_id", "evidence_kind", "source_ref",
        "evidence_hash", "facts_json", "coverage_state", "observed_at", "expires_at"}
    if not isinstance(proof, dict) or set(proof) != fields:
        raise ValueError("Invalid persisted catalog evidence record")
    expected_id = identity("AI_DG_EVIDENCE", spec["scope_id"], row["version_id"], row["metadata_hash"])
    expected = dict(evidence_id=expected_id, scope_id=spec["scope_id"], asset_version_id=row["version_id"],
        evidence_kind="CATALOG", evidence_hash=row["metadata_hash"], source_ref=row["qualified_name"])
    if (link["evidence_id"] != expected_id or canonical({key:proof[key] for key in expected}) != canonical(expected)
            or canonical(model_json(proof["facts_json"])) != canonical(facts)
            or not isinstance(proof["producer_run_id"], str) or not proof["producer_run_id"]):
        raise PermissionError("The case evidence is not the exact stored catalog observation")
    name = facts.get("table_key")
    if row["asset_kind"] == "COLUMN" and isinstance(name, str) and isinstance(facts.get("column_name"), str):
        name += "." + facts["column_name"]
    schemas = [scope["schema_key"] for scope, scope_id in zip(binding["source_scopes"], consultation_scope_ids(binding), strict=True)
        if scope_id == spec["scope_id"]]
    if (row["asset_kind"] not in {"TABLE", "COLUMN"} or name != row["qualified_name"] or not isinstance(name, str)
            or not any(name.startswith(schema + ".") for schema in schemas)):
        raise ValueError("The assessed historical source is outside the case scope")


def checked_case_evidence_time(proof, *, historical, now):
    observed = datetime.fromisoformat(proof["observed_at"])
    expires = datetime.fromisoformat(proof["expires_at"]) if proof["expires_at"] is not None else None
    if now.utcoffset() is None or observed.utcoffset() is None or expires is not None and expires.utcoffset() is None:
        raise ValueError("Case evidence timestamps require timezones")
    if not historical and (proof["coverage_state"] not in {"COMPLETE", "PARTIAL"} or observed > now or expires is not None and expires <= now):
        raise PermissionError("Case evidence is expired, future or unavailable")


def checked_case_prior_state(binding, spec, links, rows):
    previous = case_record_snapshot("AI_DG_CASE", spec["previous"])
    grouped = case_history_groups(binding, previous["case_id"], rows)
    if grouped is None or not _same(case_record_snapshot("AI_DG_CASE", grouped["CASE"][0]), previous):
        raise PermissionError("The case state changed since the assessed preview")
    if previous["scope_id"] != spec["scope_id"] or len(grouped["ASSET"]) != len(links) or any(row["admitted"] is not True for row in grouped["ASSET"]):
        raise PermissionError("The persisted case asset set is incomplete or foreign")
    stored = sorted([{key: row[key] for key in links[0]} for row in grouped["ASSET"]], key=lambda row: row["asset_id"])
    if not _same(stored, links):
        raise PermissionError("The assessed case links changed")
    head = case_decision_head(previous, grouped["DECISION"])
    expected = case_record_snapshot("AI_DG_DECISION", spec["previous_decision"]) if spec["previous_decision"] is not None else None
    if not _same(head, expected):
        raise PermissionError("The prior case decision changed")
    if expected is not None and (expected["change_digest"] != previous["change_digest"] or expected["versions_digest"] != case_versions(spec["scope_id"], links)):
        raise PermissionError("The prior decision belongs to another assessed treatment")


def case_decision_head(previous, decisions):
    previous = case_record_snapshot("AI_DG_CASE", previous)
    decisions = [case_record_snapshot("AI_DG_DECISION", row) for row in decisions]
    if any(datetime.fromisoformat(row["recorded_at"]) > datetime.fromisoformat(previous["updated_at"]) for row in decisions):
        raise PermissionError("A later case decision is pending materialization")
    heads = [row for row in decisions if row["recorded_at"] == previous["updated_at"]]
    if len(heads) > 1:
        raise PermissionError("The case has conflicting current decisions")
    return heads[0] if heads else None


class CaseReadmission:
    def __init__(self, binding, caller, query, hooks_factory, *, now):
        self.binding, self.caller, self.query, self.hooks_factory, self.now = binding, caller, query, hooks_factory, now

    async def revalidate(self, spec):
        self.caller.check_identity()
        links = case_specification_links(self.binding, spec)
        if spec["previous"] is not None:
            case_id = checked_case_reference(spec["previous"]["case_id"])
            history = await self.query(case_history_query(self.binding, case_id), limit=48)
            checked_case_prior_state(self.binding, spec, links, history)
        elif spec["action"] != "OPEN" or spec["previous_decision"] is not None:
            raise ValueError("This case action requires an exact prior state")
        if spec["action"] != "REVOKE":
            await self._current_assets(spec, links)
        rows = await self.query(case_catalog_evidence_query(self.binding, spec), limit=16)
        if not isinstance(rows, list) or len(rows) != len(links) or len({row["asset_id"] for row in rows}) != len(links):
            raise PermissionError("Persisted case evidence coverage is incomplete or duplicated")
        by_asset = {row["asset_id"]: row for row in rows}
        for link in links:
            if link["asset_id"] not in by_asset:
                raise PermissionError("A persisted case evidence row is missing")
            checked_case_catalog_proof(self.binding, spec, link, by_asset[link["asset_id"]], now=self.now())
        self.caller.check_identity()
        return True

    async def _current_assets(self, spec, links):
        hooks = self.hooks_factory()
        if hooks.rows or hooks.case_snapshot is not None:
            raise ValueError("Case readmission requires a fresh consultation admission")
        rows = await self.query(projection_search(self.binding, [], asset_ids=[row["asset_id"] for row in links]), limit=32)
        hooks.rows = hooks._checked_rows(rows)
        expected = {(row["asset_id"], row["assessed_version_id"]) for row in links}
        if (len(rows) != len(links) or {(row["asset_id"], row["version_id"]) for row in rows} != expected
                or any(row["scope_id"] != spec["scope_id"] for row in rows)):
            raise PermissionError("The current case asset/version set changed")
        await hooks.authorize({}, assets=[{"asset_id": row["asset_id"], "version_id": row["version_id"], "name": row["qualified_name"]} for row in rows])
