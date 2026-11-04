"""Join versioned Delta metadata to fresh observations from the admitted identity."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
import re
from threading import Event
from urllib.parse import quote

from .catalog import CatalogReader, CatalogScope, _check_scope, is_redacted_metadata, is_governance_control_table
from .code_usage import CallerCodeVerifier, usage_candidate, _usage_time, current_code_references
from .inventory import canonical, identity, table_records
from .lineage import LineageReader, LineageTaskReader
from .model_output import model_json
from .projection_query import consultation_scope_ids, projection_search, projection_quality, projection_evidence
from .case_reader import checked_case_reference, case_history_query, case_history_snapshot
from .central_sql import metadata_worker


PROJECTION_FIELDS = frozenset({"scope_id", "asset_id", "version_id", "asset_kind", "qualified_name", "description",
    "description_origin", "data_type", "owner_ref", "metadata_hash", "coverage", "observed_at", "table_key", "column_name"})


class SourceObservationExpired(PermissionError):
    """A retained source observation cannot support the current phase."""


class ConsultationHooks:
    capabilities = frozenset({"read_asset_snapshot", "read_lineage", "read_quality_results", "read_access_evidence", "read_case"})

    def __init__(self, binding, caller, query, *, code_usage_enabled=False, confirm_description=None,
            confirm_descriptions=None):
        if type(code_usage_enabled) is not bool:
            raise ValueError("Code usage capability requires explicit admission")
        if code_usage_enabled:
            if getattr(caller, 'code_reads_enabled', False) is not True:
                raise PermissionError("Caller source reads have not been admitted")
            self.capabilities = self.capabilities | {"find_code_usage"}
        self.binding, self.caller, self.query = binding, caller, query
        if confirm_description is not None and not callable(confirm_description):
            raise ValueError("Catalog description confirmation requires an admitted capability")
        self.confirm_description = confirm_description
        if confirm_descriptions is not None and not callable(confirm_descriptions):
            raise ValueError("Catalog batch confirmation requires an admitted capability")
        self.confirm_descriptions = confirm_descriptions
        self.catalog = CatalogReader(caller, max_pages=5)
        self.scopes = dict(zip(consultation_scope_ids(binding),
            (CatalogScope(**row) for row in binding["source_scopes"]), strict=True))
        self.rows = {}
        self.catalog_snapshots = {}
        self.case_snapshot = None

    async def resolve_assets(self, intent, config):
        self.caller.check_identity()
        if intent.clarification:
            return []
        if intent.intent in {"case_followup", "decision"} or intent.intent == "treatment" and intent.case_reference:
            if not intent.case_reference:
                raise ValueError("Case follow-up requires an exact reference")
            self.case_snapshot = await self._read_case_snapshot(intent.case_reference)
            if self.case_snapshot is None:
                return []
            if intent.intent != "treatment":
                return [self.case_snapshot["resource"]]
            return await self._case_treatment_assets(config)
        rows = await self.query(projection_search(self.binding, intent.asset_mentions,
            tables_only=intent.intent == "inventory" and not intent.asset_mentions), limit=32)
        self.rows = self._checked_rows(rows)
        assets = [{"asset_id": r["asset_id"], "version_id": r["version_id"], "name": r["qualified_name"]}
            for r in rows]
        try:
            await self.authorize(config, assets=assets)
        except (Exception, asyncio.CancelledError):
            self.rows = {}
            self.catalog_snapshots = {}
            raise
        return assets

    async def _case_treatment_assets(self, config):
        facts = self.case_snapshot["proof"]["facts"]
        if facts["omitted_asset_links"] or facts["asset_links_truncated"] or not facts["asset_links"]:
            return [self.case_snapshot["resource"]]
        links = facts["asset_links"]
        rows = await self.query(projection_search(self.binding, [], asset_ids=[row["asset_id"] for row in links]), limit=32)
        self.rows = self._checked_rows(rows)
        if {(r['asset_id'],r['version_id']) for r in rows} != {(r['asset_id'],r['assessed_version_id']) for r in links}:
            raise PermissionError("The case assessed versions changed; the case cannot be silently rebased")
        assets = [self.case_snapshot["resource"], *[{"asset_id":r['asset_id'],"version_id":r['version_id'],"name":r['qualified_name']} for r in rows]]
        await self.authorize(config, assets=assets)
        return assets

    def _checked_rows(self, rows):
        if not isinstance(rows, list) or len(rows) > 32:
            raise ValueError("Consultation projection exceeds its budget")
        checked = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != PROJECTION_FIELDS:
                raise ValueError("Invalid consultation projection fields")
            if row["scope_id"] not in self.scopes or row["asset_kind"] not in {"TABLE", "COLUMN"}:
                raise ValueError("Projection belongs to an unsupported source")
            self._check_projection_identity(row)
            self._check_projection_name(row)
            if row["asset_id"] in checked or row["coverage"] not in {"COMPLETE", "PARTIAL"}:
                raise ValueError("Duplicate or unsupported projection")
            checked[row["asset_id"]] = dict(row)
        return checked

    def _check_projection_identity(self, row):
        if not isinstance(row["asset_id"], str) or not 1 <= len(row["asset_id"]) <= 256:
            raise ValueError("Invalid projection asset identity")
        for name in ("version_id", "metadata_hash"):
            if not isinstance(row[name], str) or not re.fullmatch(r"[0-9a-f]{64}", row[name]):
                raise ValueError("Invalid projection identity")
        if row["version_id"] != identity("AI_DG_ASSET_VERSION", row["asset_id"], row["metadata_hash"]):
            raise ValueError("Projection version does not match its metadata")

    def _check_projection_name(self, row):
        table, column = row["table_key"], row["column_name"]
        if (not isinstance(table, str) or not table.startswith(self.scopes[row["scope_id"]].schema_key + ".")
                or len(table) > 1024):
            raise ValueError("Invalid projection table binding")
        if row["asset_kind"] == "COLUMN":
            if not isinstance(column, str) or not 1 <= len(column) <= 256:
                raise ValueError("Invalid projection column binding")
        elif column is not None:
            raise ValueError("A table projection cannot identify a column")
        if row["qualified_name"] != table + ("." + column if column is not None else ""):
            raise ValueError("Projection name does not match its source")

    async def authorize(self, config, *, assets):
        self.caller.check_identity()
        if not isinstance(assets, list) or len(assets) > 32:
            raise PermissionError("The consultation asset set is unavailable")
        if not assets:
            return
        cases = [asset for asset in assets if str(asset.get("asset_id", "")).startswith("case:")]
        if cases:
            await self._authorize_case(cases)
            assets = [asset for asset in assets if asset not in cases]
            admitted = {(r['asset_id'],r['assessed_version_id']) for r in self.case_snapshot['proof']['facts']['asset_links']}
            if not {(a['asset_id'],a['version_id']) for a in assets}.issubset(admitted):
                raise PermissionError("Case consultation contains an unrelated asset revision")
            if not assets:
                return
        ids = [row.get("asset_id") for row in assets]
        for asset_id in ids:
            self.catalog_snapshots.pop(asset_id, None)
        if any(key not in self.rows for key in ids):
            rows = await self.query(projection_search(self.binding, [], asset_ids=ids), limit=32)
            self.rows.update(self._checked_rows(rows))
        grouped = {}
        for asset in assets:
            row = self.rows.get(asset["asset_id"])
            if row is None or asset != {"asset_id": row["asset_id"], "version_id": row["version_id"], "name": row["qualified_name"]}:
                raise PermissionError("The admitted asset revision is no longer available")
            key = row["scope_id"], row["table_key"]
            grouped.setdefault(key, row)
        details = await self._current_tables(grouped)
        observations = {key:table_records(self.binding['platform_id'], row['scope_id'], 'consultation',
            details[key], datetime.now(timezone.utc)).versions for key, row in grouped.items()}
        verified = {}
        for asset in assets:
            row = self.rows[asset['asset_id']]
            key = row['scope_id'], row['table_key']
            current = next((v for v in observations[key] if v["qualified_name"] == row["qualified_name"]), None)
            if current is None or current["metadata_hash"] != row["metadata_hash"]:
                raise RuntimeError("Catalog metadata changed; its Governance projection needs synchronization")
            verified[asset['asset_id']] = model_json(current['metadata_json'])
        self.catalog_snapshots.update(verified)

    async def _table_observation(self, row):
        scope, table = self.scopes[row["scope_id"]], row["table_key"]
        if is_governance_control_table(table):
            raise ValueError("Governance control tables cannot document themselves")
        detail, headers = await metadata_worker(self.caller, "GET", "/tables/" + quote(table, safe=""))
        if not isinstance(detail, dict) or detail.get("key") != table:
            raise ValueError("Catalog confirmation returned a mismatched table")
        _check_scope(detail, scope)
        return detail, headers

    async def _known_description(self, row):
        parent = row
        if row["asset_kind"] == "COLUMN":
            parents = self._checked_rows(await self.query(projection_search(self.binding, [row['table_key']]), limit=32))
            parent = next(iter(parents.values())) if len(parents) == 1 else None
        if (parent is None or parent["asset_kind"] != "TABLE" or parent["scope_id"] != row["scope_id"]
                or parent["table_key"] != row['table_key'] or not isinstance(parent["description"], str)
                or not parent["description"].strip() or is_redacted_metadata(parent["description"])):
            raise ValueError("Masked catalog metadata has no known parent description")
        return parent['description']

    async def _current_tables(self, grouped):
        if self.confirm_descriptions is None:
            return {key:await self._current_table(row) for key, row in grouped.items()}
        observed, texts, checks = {}, {}, []
        for key, row in grouped.items():
            observed[key] = await self._table_observation(row)
            if is_redacted_metadata(observed[key][0].get('description')):
                text = await self._known_description(row)
                texts[key] = text
                checks.append({'table_key':row['table_key'], 'expected_hash':hashlib.sha256(text.encode()).hexdigest()})
        confirmed = {}
        if checks:
            self.caller.check_identity()
            confirmed = await self.confirm_descriptions(checks)
            self.caller.check_identity()
            if (not isinstance(confirmed, dict) or set(confirmed) != {c['table_key'] for c in checks}
                    or any(type(value) is not bool for value in confirmed.values())):
                raise ValueError('Catalog batch confirmation coverage differs')
        details = {}
        for key, row in grouped.items():
            detail, headers = observed[key]
            if key in texts and confirmed[row['table_key']] is True:
                detail = {**detail, 'description':texts[key]}
            details[key] = CatalogReader(lambda *args, **kwargs: (detail, headers)).read_table(
                self.scopes[row['scope_id']], row['table_key'])
        return details

    async def _current_table(self, row):
        detail, headers = await self._table_observation(row)
        if is_redacted_metadata(detail.get("description")) and self.confirm_description is not None:
            text = await self._known_description(row)
            expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
            self.caller.check_identity()
            if await self.confirm_description(row['table_key'], expected) is True:
                detail = {**detail, "description": text}
            self.caller.check_identity()
        return CatalogReader(lambda *args, **kwargs: (detail, headers)).read_table(
            self.scopes[row['scope_id']], row['table_key'])

    async def read(self, operation, assets, config):
        await self.authorize(config, assets=assets)
        return await self._read_admitted(operation, assets, config)

    async def _read_admitted(self, operation, assets, config):
        """Read immediately after the graph's admission for this operation."""
        self.caller.check_identity()
        if operation not in self.capabilities:
            raise ValueError("Unsupported consultation capability")
        if operation == "read_case":
            cases = [asset for asset in assets if asset["asset_id"].startswith("case:")]
            if not cases:
                return []
            if self.case_snapshot is None or cases != [self.case_snapshot["resource"]]:
                raise PermissionError("Case evidence is not bound to these resources")
            return [self.case_snapshot["proof"]]
        rows = [self.rows[asset["asset_id"]] for asset in assets if not asset["asset_id"].startswith("case:")]
        if operation == "read_lineage":
            if len(rows) > 4:
                raise ValueError("Lineage consultation requires a narrower asset selection")
            return [await asyncio.to_thread(self._lineage, row) for row in rows]
        if operation == "read_quality_results":
            return await self._quality(rows) if rows else []
        if operation == "find_code_usage":
            return await self._code_usage(rows) if rows else []
        return [self._metadata_proof(operation, row, byte_budget=32768 // max(1, len(rows))) for row in rows]

    def _metadata_proof(self, operation, row, *, byte_budget):
        facts = {key: row[key] for key in ("qualified_name", "description", "description_origin", "data_type", "owner_ref", "observed_at")}
        kind = "CATALOG"
        coverage = "complete" if row["coverage"] == "COMPLETE" else "partial"
        if operation == "read_asset_snapshot" and row["asset_kind"] == "TABLE":
            snapshot = self.catalog_snapshots.get(row['asset_id'])
            if snapshot is None or identity('metadata', snapshot) != row['metadata_hash']:
                raise ValueError('The full catalog schema has not been verified for this asset version')
            facts.update(_catalog_schema_facts(snapshot, byte_budget=byte_budget))
            if not facts['columns_complete']:
                coverage = 'partial'
        if operation == "read_access_evidence":
            facts = {"resource": row["qualified_name"], "operation": "READ_METADATA", "result": "ALLOWED",
                "observed_at": datetime.now(timezone.utc).isoformat(), "data_access_inferred": False}
            central = getattr(self.caller, "access_mode", None) == "CENTRAL_METADATA"
            facts.update(access_basis="CENTRAL_METADATA" if central else "CALLER_DELEGATED",
                caller_permissions_evaluated=not central)
            kind = "ACCESS_OBSERVATION"
        return _consultation_proof(row, kind, facts, coverage)

    async def _read_case_snapshot(self, case_id):
        self.caller.check_identity()
        rows = await self.query(case_history_query(self.binding, case_id), limit=48)
        self.caller.check_identity()
        return case_history_snapshot(self.binding, case_id, rows)

    async def _authorize_case(self, assets):
        if len(assets) != 1:
            raise PermissionError("Case history cannot mix consultation resources")
        case_id = checked_case_reference(assets[0]["asset_id"][5:])
        current = await self._read_case_snapshot(case_id)
        if current is None or assets != [current["resource"]]:
            raise RuntimeError("Case history changed; request its current state")
        self.case_snapshot = current

    async def _code_usage(self, rows):
        if len(rows) > 4:
            raise ValueError('Code usage requires a narrower asset selection')
        cancelled = Event()
        def check_active():
            if cancelled.is_set():
                raise InterruptedError("Source readmission was cancelled")
            self.caller.check_identity()
        verifier = CallerCodeVerifier(self.caller, self.binding['workspace_key'], check_active=check_active)
        try:
            return await self._read_code_usage(rows, verifier)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def _read_code_usage(self, rows, verifier):
        candidates = await self.query(projection_evidence(rows, ('STATIC_CODE',)), limit=128)
        if not isinstance(candidates, list) or len(candidates) > 128:
            raise ValueError('Source evidence exceeds its consultation budget')
        revisions = {(row['scope_id'], row['version_id']): row for row in rows}
        proofs, size, considered = [], 0, set()
        for candidate in candidates:
            admitted = usage_candidate(candidate, revisions)
            if admitted is None:
                continue
            row, refs = admitted
            verified = [await asyncio.to_thread(verifier.admit, ref, row) for ref in refs]
            allowed = [ref for ref in verified if ref is not None]
            if not allowed:
                continue
            considered.add((row["asset_id"], row["version_id"]))
            facts = {'target': row['qualified_name'], 'references': allowed, 'execution_evidence': False,
                'analysis_complete': False, 'observed_at': datetime.now(timezone.utc).isoformat(),
                'expires_at': candidate['expires_at'], 'source_evidence_id': candidate['evidence_id']}
            proof = _consultation_proof(row, 'STATIC_CODE', facts, 'partial', discriminator=candidate['evidence_id'])
            size += len(canonical(proof).encode())
            if size > 65536 or len(proofs) >= 8:
                break
            proofs.append(proof)
        missing = [row for row in rows if (row["asset_id"], row["version_id"]) not in considered]
        if missing:
            discovered = await current_code_references(self.caller, self.binding["workspace_key"], missing, verifier=verifier)
            at = datetime.now(timezone.utc)
            for row, refs in discovered:
                if not refs:
                    continue
                source_id = identity("current-code-observation", row["version_id"], refs, at.isoformat())
                facts = {"target": row["qualified_name"], "references": refs, "execution_evidence": False,
                    "analysis_complete": False, "observed_at": at.isoformat(),
                    "expires_at": (at + timedelta(minutes=10)).isoformat(), "source_evidence_id": source_id}
                proof = _consultation_proof(row, "STATIC_CODE", facts, "partial", discriminator=source_id)
                size += len(canonical(proof).encode())
                if size > 65536 or len(proofs) >= 8:
                    break
                proofs.append(proof)
        return proofs

    async def reauthorize_evidence(self, config, *, assets, evidence):
        await self.authorize(config, assets=assets)
        await self._validate_admitted_evidence(config, assets=assets, evidence=evidence)

    async def reobserve_evidence(self, config, *, assets, evidence):
        await self.authorize(config, assets=assets)
        return await self._reobserve_admitted_evidence(config, assets=assets, evidence=evidence)

    async def _reobserve_admitted_evidence(self, config, *, assets, evidence):
        self.caller.check_identity()
        static = [proof for proof in evidence if proof.get('kind') == 'STATIC_CODE']
        if 'find_code_usage' not in self.capabilities or not 1 <= len(static) <= 8:
            raise PermissionError('Source reobservation is unavailable')
        admitted = {(asset['asset_id'],asset['version_id']) for asset in assets}
        if any((proof['asset_id'],proof['version_id']) not in admitted for proof in evidence):
            raise PermissionError('Source reobservation contains an unadmitted asset')
        await self._validate_admitted_evidence(config, assets=assets,
            evidence=[proof for proof in evidence if proof.get('kind') != 'STATIC_CODE'])
        cancelled = Event()
        at = datetime.now(timezone.utc)
        await metadata_worker(self._verify_source_reobservation, static, cancelled, cancelled=cancelled)
        self.caller.check_identity()
        return [self._reobserved_proof(proof,at) for proof in evidence]

    def _verify_source_reobservation(self, static, cancelled):
        def active():
            if cancelled.is_set():
                raise InterruptedError('Source reobservation was cancelled')
            self.caller.check_identity()
        verifier = CallerCodeVerifier(self.caller, self.binding['workspace_key'], check_active=active)
        for proof in static:
            row = self.rows.get(proof.get('asset_id'))
            for ref in self._code_proof_references(proof, row):
                if not verifier.verify(ref, row):
                    raise PermissionError('The exact source observation changed or is unavailable')
        active()

    def _reobserved_proof(self, proof, at):
        if proof.get('kind') != 'STATIC_CODE':
            return proof
        row = self.rows[proof['asset_id']]
        source_id = identity('reobserved-code-observation', proof['evidence_id'], at.isoformat())
        facts = {**proof['facts'], 'observed_at':at.isoformat(),
            'expires_at':(at+timedelta(minutes=10)).isoformat(), 'source_evidence_id':source_id}
        fresh = _consultation_proof(row, 'STATIC_CODE', facts, 'partial', discriminator=source_id)
        self._checked_code_proof(fresh, row)
        return fresh

    async def _validate_admitted_evidence(self, config, *, assets, evidence):
        """Validate immediately after the graph's fresh phase authorization."""
        self.caller.check_identity()
        cases = [proof for proof in evidence if proof.get("kind") == "CASE_RECORD"]
        if cases:
            if self.case_snapshot is None or cases != [self.case_snapshot["proof"]]:
                raise PermissionError("Case history evidence changed")
        static = [proof for proof in evidence if proof.get('kind') == 'STATIC_CODE']
        if not static:
            return
        if 'find_code_usage' not in self.capabilities or len(static) > 8:
            raise PermissionError('Code usage evidence is unavailable')
        verifier = CallerCodeVerifier(self.caller, self.binding['workspace_key'])
        for proof in static:
            row = self.rows.get(proof.get('asset_id'))
            refs = self._checked_code_proof(proof, row)
            for ref in refs:
                if not await asyncio.to_thread(verifier.verify, ref, row):
                    raise PermissionError('A source observation is no longer authorized or current')
        for proof in static:
            self._checked_code_proof(proof, self.rows.get(proof.get('asset_id')))

    def _checked_code_proof(self, proof, row):
        refs = self._code_proof_references(proof, row)
        facts = proof['facts']
        if not _usage_time(facts['observed_at']) <= datetime.now(timezone.utc) < _usage_time(facts['expires_at']):
            raise SourceObservationExpired('Source observation expired')
        return refs

    def _code_proof_references(self, proof, row):
        if row is None or proof.get('version_id') != row['version_id'] or proof.get('coverage') != 'partial':
            raise PermissionError('Source observation belongs to another revision')
        facts = proof.get('facts')
        fields = {'target', 'references', 'execution_evidence', 'analysis_complete', 'observed_at', 'expires_at', 'source_evidence_id'}
        if (not isinstance(facts, dict) or set(facts) != fields or facts['target'] != row['qualified_name']
                or facts['execution_evidence'] is not False or facts['analysis_complete'] is not False):
            raise ValueError('Invalid consultation source facts')
        if not _usage_time(facts['observed_at']) < _usage_time(facts['expires_at']):
            raise ValueError('Invalid source observation window')
        if _usage_time(facts['observed_at']) > datetime.now(timezone.utc):
            raise ValueError('Source observation is in the future')
        expected = _consultation_proof(row, 'STATIC_CODE', facts, 'partial', discriminator=facts['source_evidence_id'])
        if proof != expected:
            raise ValueError('Consultation source proof changed')
        refs = facts['references']
        if not isinstance(refs, list) or not 1 <= len(refs) <= 16:
            raise ValueError('Consultation source reference budget changed')
        return refs

    def _lineage(self, row):
        visible_tables = {}
        task_reader = LineageTaskReader(self.caller, self.binding["platform_id"], self.binding["workspace_key"])
        def visible(node):
            if node.get("type") == "Task":
                return task_reader.visible(node)
            name = node.get("qualifiedName", "")
            if not isinstance(name, str):
                return False
            prefix = f"aidp://catalogs@{self.binding['platform_id']}/o/"
            table = name[len(prefix):] if name.startswith(prefix) else name
            scope = next((scope for scope in self.scopes.values() if table.startswith(scope.schema_key + ".")), None)
            if scope is None:
                return False
            if table not in visible_tables:
                if len(visible_tables) >= 16:
                    return False
                visible_tables[table] = False
                try:
                    self.catalog.read_table(scope, table)
                except (PermissionError, RuntimeError, ValueError):
                    return False
                visible_tables[table] = True
            return visible_tables[table]
        snapshot = LineageReader(self.caller, self.binding["platform_id"], admit_node=visible, max_pages=2).read(
            self.scopes[row["scope_id"]], row["table_key"], column_name=row["column_name"], depth=2)
        nodes = list(snapshot.nodes[:16])
        node_ids = {node["id"] for node in nodes}
        links = [link for link in snapshot.links if link["fromNodeId"] in node_ids and link["toNodeId"] in node_ids][:32]
        complete = (snapshot.pagination_complete and not snapshot.omitted_nodes and not snapshot.unresolved_links
            and len(nodes) == len(snapshot.nodes) and len(links) == len(snapshot.links))
        facts = {"anchor": row["qualified_name"], "nodes": nodes, "links": links,
            "pagination_complete": snapshot.pagination_complete, "omitted_nodes": snapshot.omitted_nodes,
            "unresolved_links": snapshot.unresolved_links, "traversal_depth": 2, "execution_evidence": False,
            "output_truncated": len(nodes) != len(snapshot.nodes) or len(links) != len(snapshot.links),
            "absence_proves_global_isolation": False}
        return _consultation_proof(row, "RUNTIME_LINEAGE", facts, "complete" if complete else "partial")

    async def _quality(self, rows):
        observed = await self.query(projection_quality(rows), limit=128)
        if not isinstance(observed, list) or len(observed) > 128:
            raise ValueError("Quality evidence exceeds its consultation budget")
        revisions = {(row["scope_id"], row["version_id"]): row for row in rows}
        proofs = []
        for assessment in observed:
            if assessment.get("rule_kind") != "QUALITY" or assessment.get("evidence_kind") != "QUALITY":
                raise ValueError("Non-quality evidence cannot support a quality assessment")
            row = revisions.get((assessment.get("scope_id"), assessment.get("version_id")))
            if row is None:
                raise ValueError("Quality evidence belongs to an unadmitted revision")
            result = model_json(assessment["result_json"])
            coverage = "complete" if assessment["coverage"] == "COMPLETE" else "partial"
            facts = {"asset_version_id": row["version_id"], "evidence_id": assessment["evidence_id"],
                "rule_id": assessment["rule_id"], "rule_version": assessment["rule_version"],
                "status": assessment["status"], "result": result,
                "measured_at": assessment["measured_at"], "expires_at": assessment["expires_at"]}
            proofs.append(_consultation_proof(row, "QUALITY_ASSESSMENT", facts, coverage,
                discriminator=assessment["assessment_id"]))
        return proofs


def _catalog_schema_facts(snapshot, *, byte_budget):
    columns = snapshot['columns']
    selected, size = [], 2
    for column in (columns or ())[:128]:
        width = len(canonical(column).encode('utf-8')) + 1
        if size + width > byte_budget:
            break
        selected.append(dict(column))
        size += width
    count = len(columns) if columns is not None else None
    return {'columns': selected if columns is not None else None, 'column_count': count,
        'columns_omitted': count - len(selected) if count is not None else None,
        'columns_complete': columns is not None and len(selected) == count}


def _consultation_proof(row, kind, facts, coverage, *, discriminator=None):
    return {"evidence_id": identity("consultation-proof", row["scope_id"], row["version_id"], kind, discriminator, facts),
        "asset_id": row["asset_id"], "version_id": row["version_id"], "kind": kind, "facts": facts, "coverage": coverage}
