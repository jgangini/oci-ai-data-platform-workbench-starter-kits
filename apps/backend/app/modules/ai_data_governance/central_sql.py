"""Central OCI signing for bounded SQL, using the installed Data Flow client."""
from __future__ import annotations

import asyncio
import os
import re
from threading import Event
from urllib.parse import urlsplit

from .inventory import canonical
from .catalog import is_governance_control_table
from .model_output import model_json
from .service_transport import runtime_sdk_config


async def metadata_worker(function, *args, cancelled=None):
    """Keep bounded metadata workers alive until their client can safely close."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if cancelled is not None:
            cancelled.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


def _central_sql_endpoint(config):
    endpoint = os.getenv("AIDP_GATEWAY_ENDPOINT", "")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith("." + config["region"] + ".oci.oraclecloud.com")
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
        raise ValueError("A bound regional AIDP SQL endpoint is required")
    return endpoint.rstrip("/")


class CentralSqlSdk:
    """Keep native session context out of central service authorization headers."""

    def __init__(self, client, cluster, check_identity):
        self.client, self.cluster, self.check_identity = client, cluster, check_identity
        self.contexts = set()

    def _call(self, operation, *args, **kwargs):
        self.check_identity()
        kwargs.pop("dh_user_principal", None)
        if not args or (args[1] if operation == "create_execution_context" else args[0]) != self.cluster:
            raise ValueError("SQL request escaped its bound compute")
        if operation != "create_execution_context" and (len(args) < 2 or args[1] not in self.contexts):
            raise ValueError("SQL context is not owned by this invocation")
        response = getattr(self.client, operation)(*args, **kwargs)
        if operation == "create_execution_context":
            key = getattr(response.data, "id", None)
            if not isinstance(key, str) or not key or len(key) > 256:
                raise RuntimeError("SQL context creation returned no usable identity")
            self.contexts.add(key)
        elif operation == "delete_context":
            self.contexts.remove(args[1])
        self.check_identity()
        return response

    def create_execution_context(self, details, cluster_id, **kwargs):
        return self._call("create_execution_context", details, cluster_id, **kwargs)

    def get_context_status(self, cluster_id, context_id, **kwargs):
        return self._call("get_context_status", cluster_id, context_id, **kwargs)

    def execute_command(self, details, cluster_id, context_id, **kwargs):
        # The SDK puts details first for command execution as well.
        self.check_identity()
        if cluster_id != self.cluster or context_id not in self.contexts:
            raise ValueError("SQL command escaped its owned context")
        kwargs.pop("dh_user_principal", None)
        response = self.client.execute_command(details, cluster_id, context_id, **kwargs)
        self.check_identity()
        return response

    def get_command_status(self, cluster_id, context_id, command_id, **kwargs):
        return self._call("get_command_status", cluster_id, context_id, command_id, **kwargs)

    def delete_context(self, cluster_id, context_id, **kwargs):
        return self._call("delete_context", cluster_id, context_id, **kwargs)

    def cleanup(self):
        # Cleanup uses the already-bound service signer even after caller cancellation.
        for context_id in tuple(self.contexts):
            self.client.delete_context(self.cluster, context_id, x_aidp_source="compute")
            self.contexts.remove(context_id)


def run_central_projection(config, signer, sql, check_identity):
    return _run_central_command(config, signer, sql, check_identity, language="SQL")


def _observe_central_command(diagnose, stage, transition, error=None):
    if diagnose is not None:
        try:
            diagnose(stage, transition, error)
        except Exception:
            # Diagnostics never replace the command or cleanup outcome.
            pass


def _run_central_command(config, signer, command, check_identity, *, language, max_poll_attempts=40, diagnose=None):
    if type(max_poll_attempts) is not int or max_poll_attempts not in (40,120):
        raise ValueError('Only the ordinary or fixed-case observation bound is supported')
    _observe_central_command(diagnose, 'command_setup', 'started')
    try:
        import aidputils
        from aidputils.agents.tools.dataflow.client import DataflowCommandClient
        from oci.dataflowdp import DataflowdpClient
        from oci.retry import NoneRetryStrategy

        endpoint = _central_sql_endpoint(config)
        check_identity()
        client_config = runtime_sdk_config(config, signer, aidputils.secrets.get)
        sdk = DataflowdpClient(client_config, signer=signer, service_endpoint=endpoint,
            timeout=(10, 30), retry_strategy=NoneRetryStrategy())
    except Exception as exc:
        _observe_central_command(diagnose, 'command_setup', 'failed', exc)
        raise
    _observe_central_command(diagnose, 'command_setup', 'completed')
    central = CentralSqlSdk(sdk, config["query_compute_key"], check_identity)
    try:
        _observe_central_command(diagnose, 'command', 'started')
        try:
            client = DataflowCommandClient(endpoint=endpoint, signer=signer, oci_sdk_client=central,
                workspace_key=config["workspace_key"], context_cache_ttl_secs=0,
                poll_interval_secs=3, max_poll_attempts=max_poll_attempts, timeout_secs=30)
            result = client.run_command(cluster_id=config["query_compute_key"], language=language,
                command=command, cleanup_context=True)
        except Exception as exc:
            _observe_central_command(diagnose, 'command', 'failed', exc)
            raise
        _observe_central_command(diagnose, 'command', 'completed')
        _observe_central_command(diagnose, 'command_identity', 'started')
        try:
            check_identity()
        except Exception as exc:
            _observe_central_command(diagnose, 'command_identity', 'failed', exc)
            raise
        _observe_central_command(diagnose, 'command_identity', 'completed')
        return result
    finally:
        try:
            _observe_central_command(diagnose, 'cleanup', 'started')
            try:
                central.cleanup()
            except Exception as exc:
                _observe_central_command(diagnose, 'cleanup', 'failed', exc)
                raise
            _observe_central_command(diagnose, 'cleanup', 'completed')
        finally:
            _observe_central_command(diagnose, 'session_close', 'started')
            try:
                sdk.base_client.session.close()
            except Exception as exc:
                _observe_central_command(diagnose, 'session_close', 'failed', exc)
                raise
            _observe_central_command(diagnose, 'session_close', 'completed')


def _description_confirmation_target(config, table_key, text_hash):
    if (not isinstance(table_key, str) or not 1 <= len(table_key) <= 1024
            or is_governance_control_table(table_key) or not isinstance(text_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", text_hash)):
        raise ValueError("An admitted table and expected description hash are required")
    scopes = [s for s in config["source_scopes"] if table_key.startswith(s["schema_key"] + ".")]
    if len(scopes) != 1:
        raise ValueError("Description confirmation escaped its bound source scope")
    scope = scopes[0]
    catalog = scope["catalog_key"]
    if not scope["schema_key"].startswith(catalog + "."):
        raise ValueError("Description confirmation schema is not bound to its catalog")
    schema = scope["schema_key"][len(catalog) + 1:]
    table = table_key[len(scope["schema_key"]) + 1:]
    if not schema or not table:
        raise ValueError("Description confirmation requires a complete table name")
    sql_name = ".".join("`" + part.replace("`", "``") + "`" for part in (catalog, schema, table))
    return catalog, schema, table, sql_name


def _description_confirmation_command(config, table_key, text_hash):
    catalog, schema, table, sql_name = _description_confirmation_target(config, table_key, text_hash)
    warmup = "SHOW TBLPROPERTIES " + sql_name + " ('description')"
    return ("import json, hashlib\nfrom pyspark.sql.utils import toJArray\n"
        f"spark.sql({warmup!r}).limit(0).collect()\n"
        f"catalog = spark._jsparkSession.sessionState().catalogManager().catalog({catalog!r})\n"
        f"namespace = toJArray(spark.sparkContext._gateway, spark._jvm.java.lang.String, [{schema!r}])\n"
        f"identifier = spark._jvm.org.apache.spark.sql.connector.catalog.Identifier.of(namespace, {table!r})\n"
        "value = catalog.loadTable(identifier).properties().get('description')\n"
        f"matches = isinstance(value, str) and len(value) <= 2000 and hashlib.sha256(value.encode('utf-8')).hexdigest() == {text_hash!r}\n"
        f"print(json.dumps({{'table_key': {table_key!r}, 'expected_hash': {text_hash!r}, 'matches': matches}}, sort_keys=True))\n")


def confirm_central_description(config, signer, table_key, text_hash, check_identity):
    command = _description_confirmation_command(config, table_key, text_hash)
    result = _run_central_command(config, signer, command, check_identity, language="PYTHON")
    observed = _catalog_confirmation_output(result, max_bytes=4096)
    if (set(observed) != {"table_key", "expected_hash", "matches"} or observed["table_key"] != table_key
            or observed["expected_hash"] != text_hash or type(observed["matches"]) is not bool):
        raise ValueError("Catalog hash confirmation identity differs")
    return observed["matches"]


def _catalog_confirmation_output(result, *, max_bytes):
    if (not isinstance(result, dict) or result.get("status") != "SUCCEEDED" or result.get("error")
            or result.get("results_truncated", False) is not False):
        raise RuntimeError("Catalog hash confirmation did not complete")
    outputs = result.get("outputs")
    if (not isinstance(outputs, list) or len(outputs) != 1 or not isinstance(outputs[0], dict)
            or outputs[0].get("type") != "TEXT_PLAIN" or outputs[0].get("is_base64", False) is not False):
        raise ValueError("Expected exactly one catalog hash confirmation")
    return model_json(outputs[0].get("value"), max_bytes=max_bytes)


def _description_confirmation_checks(config, checks):
    if not isinstance(checks, list) or len(checks) > 32:
        raise ValueError("Catalog hash confirmation exceeds its table budget")
    targets, keys = [], set()
    for check in checks:
        if not isinstance(check, dict) or set(check) != {'table_key','expected_hash'}:
            raise ValueError("Invalid catalog hash confirmation request")
        table, digest = check['table_key'], check['expected_hash']
        catalog, schema, name, sql_name = _description_confirmation_target(config, table, digest)
        if table in keys:
            raise ValueError("Duplicate catalog hash confirmation target")
        keys.add(table)
        targets.append({'table_key':table, 'expected_hash':digest, 'catalog':catalog,
            'schema':schema, 'name':name, 'sql_name':sql_name})
    return targets


def confirm_central_descriptions(config, signer, checks, check_identity):
    targets = _description_confirmation_checks(config, checks)
    if not targets:
        return {}
    command = ("import json, hashlib\nfrom pyspark.sql.utils import toJArray\n"
        f"targets = json.loads({canonical(targets)!r})\nobserved = []\n"
        "for target in targets:\n"
        "    spark.sql('SHOW TBLPROPERTIES ' + target['sql_name'] + \" ('description')\").limit(0).collect()\n"
        "    catalog = spark._jsparkSession.sessionState().catalogManager().catalog(target['catalog'])\n"
        "    namespace = toJArray(spark.sparkContext._gateway, spark._jvm.java.lang.String, [target['schema']])\n"
        "    identifier = spark._jvm.org.apache.spark.sql.connector.catalog.Identifier.of(namespace, target['name'])\n"
        "    value = catalog.loadTable(identifier).properties().get('description')\n"
        "    matches = isinstance(value, str) and len(value) <= 2000 and hashlib.sha256(value.encode('utf-8')).hexdigest() == target['expected_hash']\n"
        "    observed.append({'table_key':target['table_key'], 'expected_hash':target['expected_hash'], 'matches':matches})\n"
        "print(json.dumps({'results':observed}, sort_keys=True))\n")
    result = _run_central_command(config, signer, command, check_identity, language="PYTHON")
    envelope = _catalog_confirmation_output(result, max_bytes=524288)
    if set(envelope) != {'results'}:
        raise ValueError("Unexpected catalog hash confirmation fields")
    observed = envelope['results']
    expected = {row['table_key']:row['expected_hash'] for row in targets}
    confirmed = {}
    if not isinstance(observed, list) or len(observed) != len(targets):
        raise ValueError("Catalog hash confirmation coverage differs")
    for row in observed:
        if (not isinstance(row, dict) or set(row) != {'table_key','expected_hash','matches'}
                or not isinstance(row['table_key'], str) or row['table_key'] not in expected
                or row['table_key'] in confirmed or row['expected_hash'] != expected[row['table_key']]
                or type(row['matches']) is not bool):
            raise ValueError("Catalog hash confirmation identity differs")
        confirmed[row['table_key']] = row['matches']
    return confirmed


async def native_descriptions_confirmation(config, checks, *, transport):
    if getattr(transport, 'access_mode', None) != 'CENTRAL_METADATA':
        raise PermissionError("The installed central metadata identity is required")
    cancelled = Event()
    def check_active():
        if cancelled.is_set():
            raise InterruptedError("Catalog hash confirmations were cancelled")
        transport.check_identity()
    check_active()
    result = await metadata_worker(confirm_central_descriptions, config,
        transport.signer, checks, check_active, cancelled=cancelled)
    check_active()
    return result


async def native_description_confirmation(config, table_key, text_hash, *, transport):
    if getattr(transport, "access_mode", None) != "CENTRAL_METADATA":
        raise PermissionError("The installed central metadata identity is required")
    cancelled = Event()
    def check_active():
        if cancelled.is_set():
            raise InterruptedError("Catalog hash confirmation was cancelled")
        transport.check_identity()
    check_active()
    result = await metadata_worker(confirm_central_description, config, transport.signer,
        table_key, text_hash, check_active, cancelled=cancelled)
    check_active()
    return result


def central_projection_result(result):
    """Require one complete, single-column projection instead of guessing rows."""
    if (not isinstance(result, dict) or result.get("status") != "SUCCEEDED"
            or result.get("error") or result.get("results_truncated")):
        raise RuntimeError("Central SQL command did not return a complete result")
    outputs = result.get("json_outputs")
    if not isinstance(outputs, list) or len(outputs) != 1 or not isinstance(outputs[0], dict):
        raise ValueError("Expected exactly one SQL projection output")
    # SDK timestamps/events are auxiliary Python values, not projection JSON.
    envelope = {key: result[key] for key in ("metadata", "rows_fetched", "results_truncated") if key in result}
    if len(canonical({**envelope, "json_outputs": outputs}).encode()) > 2097152:
        raise ValueError("Central SQL projection exceeds its bound")
    output = outputs[0]
    rows = _central_projection_rows(output)
    return {**envelope, "result": {"structuredContent": {**output, "rows": rows}}}


def _central_projection_rows(output):
    schema, values = output.get("schema"), output.get("values")
    if (not isinstance(schema, list) or len(schema) != 1 or not isinstance(schema[0], dict)
            or schema[0].get("field", schema[0].get("name")) not in ("payload", "PAYLOAD")
            or not isinstance(values, list)):
        raise ValueError("Invalid central SQL projection schema")
    rows = []
    for value in values:
        if isinstance(value, list) and len(value) == 1:
            rows.append({"payload": value[0]})
        elif isinstance(value, dict) and len(value) == 1 and next(iter(value)) in ("payload", "PAYLOAD"):
            rows.append({"payload": next(iter(value.values()))})
        else:
            raise ValueError("Invalid central SQL projection row")
    return rows
