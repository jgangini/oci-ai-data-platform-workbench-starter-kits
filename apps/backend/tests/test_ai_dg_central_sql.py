import asyncio
import copy
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai_data_governance import central_sql, projection_query


def command_result(values=None):
    return {"status": "SUCCEEDED", "json_outputs": [
        {"schema": [{"field": "payload"}], "values": values if values is not None else [["{\"asset_id\":\"a\"}"]]}]}


def test_sdk_uses_only_bound_compute_context_and_removes_delegated_principal():
    sdk = Mock()
    sdk.create_execution_context.return_value = SimpleNamespace(data=SimpleNamespace(id="own-context"))
    check = Mock()
    client = central_sql.CentralSqlSdk(sdk, "compute", check)
    client.create_execution_context("details", "compute", dh_user_principal="native-user", opc_request_id="request")
    client.get_context_status("compute", "own-context", dh_user_principal="native-user")
    client.execute_command("details", "compute", "own-context", dh_user_principal="native-user")
    client.get_command_status("compute", "own-context", "command", dh_user_principal="native-user")
    for call in sdk.mock_calls:
        assert "dh_user_principal" not in call.kwargs
    assert sdk.create_execution_context.call_args.kwargs["opc_request_id"] == "request"
    with pytest.raises(ValueError):
        client.execute_command("details", "compute", "foreign-context")
    with pytest.raises(ValueError):
        client.get_context_status("other-compute", "own-context")
    check.side_effect = PermissionError("changed caller")
    with pytest.raises(PermissionError):
        client.get_context_status("compute", "own-context")
    client.cleanup()
    sdk.delete_context.assert_called_once_with("compute", "own-context", x_aidp_source="compute")
    assert not client.contexts


def test_successful_native_context_delete_prevents_duplicate_cleanup():
    sdk = Mock()
    sdk.create_execution_context.return_value = SimpleNamespace(data=SimpleNamespace(id="own"))
    c = central_sql.CentralSqlSdk(sdk, "compute", Mock())
    c.create_execution_context("details", "compute")
    c.delete_context("compute", "own", dh_user_principal="caller")
    c.cleanup()
    sdk.delete_context.assert_called_once_with("compute", "own")


@pytest.mark.parametrize("failure", [None, TimeoutError("activation failed")])
@pytest.mark.parametrize('attempts',[40,120])
def test_native_client_receives_same_signer_and_cleans_context_even_if_activation_fails(monkeypatch, failure,attempts):
    sdk = Mock()
    sdk.create_execution_context.return_value = SimpleNamespace(data=SimpleNamespace(id="own"))
    sdk_factory = Mock(return_value=sdk)
    signer = object()
    complete_config = {"region":"us-chicago-1", "tenancy":"fixture-tenancy", "user":"fixture-user", "fingerprint":"fixture-fingerprint", "key_content":"fixture-private"}
    sdk_config = Mock(return_value=complete_config)
    getter = Mock()
    monkeypatch.setattr(central_sql, "runtime_sdk_config", sdk_config)
    monkeypatch.setitem(sys.modules, "aidputils", SimpleNamespace(secrets=SimpleNamespace(get=getter)))
    recorded = {}
    class NativeClient:
        def __init__(self, **kw):
            recorded.update(kw)
        def run_command(self, **kw):
            recorded["command"] = kw
            recorded["oci_sdk_client"].create_execution_context("details", "compute", dh_user_principal="caller")
            if failure:
                raise failure
            return command_result()
    monkeypatch.setitem(sys.modules, "aidputils.agents.tools.dataflow.client", SimpleNamespace(DataflowCommandClient=NativeClient))
    monkeypatch.setitem(sys.modules, "oci.dataflowdp", SimpleNamespace(DataflowdpClient=sdk_factory))
    monkeypatch.setitem(sys.modules, "oci.retry", SimpleNamespace(NoneRetryStrategy=lambda: "no-retry"))
    monkeypatch.setenv("AIDP_GATEWAY_ENDPOINT", "https://gateway.aidp.us-chicago-1.oci.oraclecloud.com")
    config = {"region": "us-chicago-1", "query_compute_key": "compute", "workspace_key": "workspace"}
    if failure:
        with pytest.raises(TimeoutError):
            central_sql._run_central_command(config,signer,'fixed command',Mock(),language='PYTHON',max_poll_attempts=attempts)
    else:
        assert central_sql._run_central_command(config,signer,'fixed command',Mock(),language='PYTHON',max_poll_attempts=attempts)==command_result()
    assert sdk_factory.call_args.args[0] is complete_config
    sdk_config.assert_called_once_with(config, signer, getter)
    assert sdk_factory.call_args.kwargs["signer"] is signer and recorded["signer"] is signer
    assert recorded["context_cache_ttl_secs"] == 0
    assert recorded['max_poll_attempts']==attempts and recorded['poll_interval_secs']==3
    assert recorded["command"]["cleanup_context"] is True
    assert sdk_factory.call_args.kwargs["retry_strategy"] == "no-retry"
    sdk.delete_context.assert_called_once_with("compute", "own", x_aidp_source="compute")
    sdk.base_client.session.close.assert_called_once()


@pytest.mark.parametrize('attempts',[True,0,1,121,'120',120.0])
def test_command_observation_rejects_an_open_ended_or_coerced_bound_before_native_clients(attempts):
    with pytest.raises(ValueError,match='observation bound'):
        central_sql._run_central_command({},object(),'fixed',Mock(),language='PYTHON',max_poll_attempts=attempts)


def test_ordinary_projection_retains_its_existing_observation_bound(monkeypatch):
    command=Mock(return_value=command_result())
    monkeypatch.setattr(central_sql,'_run_central_command',command)
    config,signer,check={},object(),Mock()
    central_sql.run_central_projection(config,signer,'SELECT 1',check)
    command.assert_called_once_with(config,signer,'SELECT 1',check,language='SQL')


def test_cleanup_failure_retains_owned_context_and_is_not_success():
    sdk = Mock()
    sdk.create_execution_context.return_value = SimpleNamespace(data=SimpleNamespace(id="own"))
    sdk.delete_context.side_effect = TimeoutError("cleanup uncertain")
    c = central_sql.CentralSqlSdk(sdk, "compute", Mock())
    c.create_execution_context("details", "compute")
    with pytest.raises(TimeoutError):
        c.cleanup()
    assert c.contexts == {"own"}


def test_cancelled_cleanup_preserves_the_native_compute_route_without_user_delegation():
    sdk = Mock()
    sdk.create_execution_context.return_value = SimpleNamespace(data=SimpleNamespace(id='owned'))
    def delete(cluster, context, **kwargs):
        assert (cluster, context) == ('compute', 'owned')
        if kwargs.get('x_aidp_source') != 'compute':
            raise RuntimeError('The execution context is not visible on the default route')
        assert 'dh_user_principal' not in kwargs
    sdk.delete_context.side_effect = delete
    check = Mock()
    client = central_sql.CentralSqlSdk(sdk, 'compute', check)
    client.create_execution_context('details', 'compute', x_aidp_source='compute',
        dh_user_principal='caller')
    check.side_effect = InterruptedError('cancelled')
    client.cleanup()
    assert not client.contexts
    sdk.delete_context.assert_called_once_with('compute', 'owned', x_aidp_source='compute')


@pytest.mark.parametrize("endpoint", ["http://gateway.aidp.us-chicago-1.oci.oraclecloud.com", "https://foreign.example", "https://gateway.aidp.us-ashburn-1.oci.oraclecloud.com", "https://user:secret@gateway.aidp.us-chicago-1.oci.oraclecloud.com", "https://gateway.aidp.us-chicago-1.oci.oraclecloud.com?token=example"])
def test_endpoint_cannot_redirect_central_identity(monkeypatch, endpoint):
    monkeypatch.setenv("AIDP_GATEWAY_ENDPOINT", endpoint)
    with pytest.raises(ValueError):
        central_sql._central_sql_endpoint({"region": "us-chicago-1"})


def test_complete_native_tabular_results_decode_and_preserve_unknown_values():
    assert projection_query.decode_projection_rows(central_sql.central_projection_result(command_result()), 1) == [{"asset_id":"a"}]
    assert projection_query.decode_projection_rows(central_sql.central_projection_result(command_result([])), 1) == []


@pytest.mark.parametrize("change", [
    {"status":"FAILED"}, {"status":"RUNNING"}, {"json_outputs":[]},
    {"json_outputs":[{"schema":[],"values":[]}]},
    {"json_outputs":[{"schema":[{"field":"payload"}],"values":[["{}","extra"]]}]},
    {"metadata":{"rows_fetched":1}}, {"metadata":{"results_truncated":True}},
    {"json_outputs":[{"schema":[{"field":"payload"}],"values":[],"rows_fetched":1}]},
])
def test_missing_failed_or_incomplete_sql_results_are_not_empty_evidence(change):
    result = {**command_result([]), **copy.deepcopy(change)}
    with pytest.raises((RuntimeError, ValueError)):
        projection_query.decode_projection_rows(central_sql.central_projection_result(result), 1)


def test_native_query_forwards_service_signer_not_a_user_credential(monkeypatch):
    signer = object()
    transport = SimpleNamespace(signer=signer, access_mode="CENTRAL_METADATA", check_identity=Mock())
    run = Mock(return_value=command_result())
    monkeypatch.setattr(projection_query, "run_central_projection", run)
    assert asyncio.run(projection_query.native_projection_query({}, "SELECT payload FROM controls LIMIT 2", limit=1, transport=transport)) == [{"asset_id":"a"}]
    assert run.call_args.args[1] is signer
    before = run.call_count
    transport.access_mode = "DELEGATED"
    with pytest.raises(PermissionError):
        asyncio.run(projection_query.native_projection_query({}, "SELECT payload FROM controls LIMIT 2", limit=1, transport=transport))
    assert run.call_count == before
    transport.access_mode = "CENTRAL_METADATA"
    with pytest.raises(ValueError):
        asyncio.run(projection_query.native_projection_query({}, "SELECT payload FROM controls", limit=1, transport=transport))
    assert run.call_count == before


def test_cancellation_during_activation_prevents_command_and_cleans_owned_context(monkeypatch):
    from threading import Event
    started, release, cleaned = Event(), Event(), Event()
    sdk = Mock()
    sdk.create_execution_context.return_value = SimpleNamespace(data=SimpleNamespace(id="own"))
    signer = object()
    transport = SimpleNamespace(signer=signer, access_mode="CENTRAL_METADATA", check_identity=Mock())
    def run(config, supplied_signer, sql, check):
        client = central_sql.CentralSqlSdk(sdk, "compute", check)
        try:
            client.create_execution_context("details", "compute")
            started.set()
            if not release.wait(3):
                raise TimeoutError("fixture did not release activation")
            client.execute_command("details", "compute", "own")
            return command_result()
        finally:
            client.cleanup()
            cleaned.set()
    monkeypatch.setattr(projection_query, "run_central_projection", run)
    async def cancel_activation():
        task = asyncio.create_task(projection_query.native_projection_query({}, "SELECT payload FROM controls LIMIT 2", limit=1, transport=transport))
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert await asyncio.to_thread(cleaned.wait, 3)
    asyncio.run(cancel_activation())
    sdk.execute_command.assert_not_called()
    sdk.delete_context.assert_called_once_with("compute", "own", x_aidp_source="compute")


def test_sdk_datetime_metadata_does_not_hide_an_otherwise_complete_projection():
    from datetime import datetime, timezone
    result = {**command_result(), "time_created":datetime(2026,11,1,tzinfo=timezone.utc),
              "events":[{"time":datetime(2026,11,1,tzinfo=timezone.utc)}]}
    assert projection_query.decode_projection_rows(central_sql.central_projection_result(result),1)==[{"asset_id":"a"}]


def metadata_binding():
    return {"source_scopes": [{"catalog_key": "catalog", "schema_key": "catalog.schema"}]}


def hash_result(**change):
    value = {"table_key": "catalog.schema.orders", "expected_hash": "a" * 64, "matches": True}
    return {"status": "SUCCEEDED", "outputs": [{"type": "TEXT_PLAIN", "value": json.dumps({**value, **change})}]}


def test_description_confirmation_initializes_catalog_without_exporting_text_or_business_rows(monkeypatch):
    run = Mock(return_value=hash_result())
    monkeypatch.setattr(central_sql, "_run_central_command", run)
    signer, check = object(), Mock()
    assert central_sql.confirm_central_description(metadata_binding(), signer, "catalog.schema.orders", "a" * 64, check) is True
    args = run.call_args.args
    assert args[1] is signer and args[3] is check and run.call_args.kwargs == {"language": "PYTHON"}
    command = args[2]
    assert command.index("SHOW TBLPROPERTIES") < command.index("catalogManager()")
    assert ".limit(0).collect()" in command and "getOrCreate" not in command
    assert "print(value)" not in command and "read.table" not in command
    compile(command, "metadata_confirmation.py", "exec")
    run.return_value = hash_result(matches=False)
    assert central_sql.confirm_central_description(metadata_binding(), signer, "catalog.schema.orders", "a" * 64, check) is False


@pytest.mark.parametrize("table,digest", [("foreign.schema.orders", "a" * 64),
    ("catalog.schema.orders", "unbound"), ("catalog.schema.", "a" * 64)])
def test_unbound_metadata_confirmation_never_starts_native_command(monkeypatch, table, digest):
    run = Mock()
    monkeypatch.setattr(central_sql, "_run_central_command", run)
    with pytest.raises(ValueError):
        central_sql.confirm_central_description(metadata_binding(), object(), table, digest, Mock())
    run.assert_not_called()


@pytest.mark.parametrize("change", [{"matches": 1}, {"matches": "true"}, {"table_key": "catalog.schema.foreign"},
    {"expected_hash": "b" * 64}, {"private_text": "not admitted"}])
def test_foreign_or_malformed_hash_results_do_not_confirm(monkeypatch, change):
    monkeypatch.setattr(central_sql, "_run_central_command", Mock(return_value=hash_result(**change)))
    with pytest.raises(ValueError):
        central_sql.confirm_central_description(metadata_binding(), object(), "catalog.schema.orders", "a" * 64, Mock())


@pytest.mark.parametrize("change", [{"status": "FAILED"}, {"results_truncated": True}, {"outputs": []},
    {"outputs": [{"type": "TEXT_PLAIN", "value": "{}"}]}, {"error": "failed"}])
def test_incomplete_native_metadata_response_is_not_confirmation(monkeypatch, change):
    monkeypatch.setattr(central_sql, "_run_central_command", Mock(return_value={**hash_result(), **change}))
    with pytest.raises((RuntimeError, ValueError)):
        central_sql.confirm_central_description(metadata_binding(), object(), "catalog.schema.orders", "a" * 64, Mock())


def batch_checks():
    return [{'table_key':'catalog.schema.orders', 'expected_hash':'a'*64},
        {'table_key':'catalog.schema.shipping', 'expected_hash':'b'*64}]


def batch_result(rows=None):
    rows = [{**row, 'matches':True} for row in batch_checks()] if rows is None else rows
    return {'status':'SUCCEEDED', 'outputs':[{'type':'TEXT_PLAIN',
        'value':json.dumps({'results':rows})}]}


def test_hash_batch_uses_one_owned_command_and_accepts_only_exact_results(monkeypatch):
    rows = [{**row, 'matches':index == 0} for index, row in enumerate(batch_checks())]
    run = Mock(return_value=batch_result(rows))
    monkeypatch.setattr(central_sql, '_run_central_command', run)
    signer, check = object(), Mock()
    assert central_sql.confirm_central_descriptions(metadata_binding(), signer, batch_checks(), check) == {
        'catalog.schema.orders':True, 'catalog.schema.shipping':False}
    run.assert_called_once()
    args = run.call_args.args
    assert args[1] is signer and args[3] is check and run.call_args.kwargs == {'language':'PYTHON'}
    command = args[2]
    compile(command, 'batch_hash_confirmation.py', 'exec')
    assert '.limit(0).collect()' in command and 'getOrCreate' not in command and 'read.table' not in command
    assert command.count('print(') == 1 and 'print(value)' not in command
    run.reset_mock()
    assert central_sql.confirm_central_descriptions(metadata_binding(), signer, [], check) == {}
    run.assert_not_called()


def test_full_admitted_unicode_batch_fits_the_closed_output_budget(monkeypatch):
    checks = [{'table_key':'catalog.schema.' + '\U0001f600' * 990 + str(index),
        'expected_hash':'a'*64} for index in range(32)]
    result = batch_result([{**row,'matches':True} for row in checks])
    assert 65536 < len(result['outputs'][0]['value'].encode()) < 524288
    run = Mock(return_value=result)
    monkeypatch.setattr(central_sql, '_run_central_command', run)
    assert central_sql.confirm_central_descriptions(metadata_binding(), object(), checks, Mock()) == {
        row['table_key']:True for row in checks}
    run.assert_called_once()


def test_generated_hash_batch_executes_only_metadata_reads_and_exports_only_boolean_proofs(monkeypatch):
    import hashlib
    import io
    from contextlib import redirect_stdout
    checks = [{'table_key':'catalog.schema.orders','expected_hash':hashlib.sha256(b'private-orders').hexdigest()},
        {'table_key':'catalog.schema.shipping','expected_hash':hashlib.sha256(b'private-expected').hexdigest()}]
    run = Mock(return_value=batch_result([{**row,'matches':True} for row in checks]))
    monkeypatch.setattr(central_sql, '_run_central_command', run)
    central_sql.confirm_central_descriptions(metadata_binding(), object(), checks, Mock())
    command = run.call_args.args[2]
    spark, catalog = Mock(), Mock()
    spark._jsparkSession.sessionState.return_value.catalogManager.return_value.catalog.return_value = catalog
    spark._jvm.org.apache.spark.sql.connector.catalog.Identifier.of.side_effect = lambda namespace,name: (namespace[0],name)
    texts = {'orders':'private-orders','shipping':'private-changed'}
    catalog.loadTable.side_effect = lambda key: SimpleNamespace(properties=lambda: {'description':texts[key[1]]})
    monkeypatch.setitem(sys.modules,'pyspark.sql.utils',SimpleNamespace(toJArray=lambda gateway,kind,values:values))
    output = io.StringIO()
    with redirect_stdout(output):exec(compile(command,'metadata_batch.py','exec'),{'spark':spark})
    observed = json.loads(output.getvalue())
    assert observed == {'results':[{**checks[0],'matches':True},{**checks[1],'matches':False}]}
    assert 'private-' not in output.getvalue() and 'private-' not in command
    assert spark.sql.call_count == 2
    assert all(call.args[0].startswith('SHOW TBLPROPERTIES ') for call in spark.sql.call_args_list)
    assert spark.sql.return_value.limit.call_args_list == [((0,),),((0,),)]
    assert catalog.loadTable.call_count == 2


@pytest.mark.parametrize('change', ['missing','duplicate','foreign','hash','integer','string','extra','not-list'])
def test_hash_batch_rejects_incomplete_or_forged_results(monkeypatch, change):
    rows = [{**row, 'matches':True} for row in batch_checks()]
    if change == 'missing':rows.pop()
    elif change == 'duplicate':rows[1] = rows[0]
    elif change == 'foreign':rows[1]['table_key'] = 'catalog.schema.foreign'
    elif change == 'hash':rows[1]['expected_hash'] = 'c'*64
    elif change == 'integer':rows[1]['matches'] = 1
    elif change == 'string':rows[1]['matches'] = 'true'
    elif change == 'extra':rows[1]['private_text'] = 'not admitted'
    elif change == 'not-list':rows = {}
    monkeypatch.setattr(central_sql, '_run_central_command', Mock(return_value=batch_result(rows)))
    with pytest.raises(ValueError):
        central_sql.confirm_central_descriptions(metadata_binding(), object(), batch_checks(), Mock())


@pytest.mark.parametrize('change', ['duplicate','foreign','large','fields'])
def test_invalid_hash_batch_never_starts_a_native_command(monkeypatch, change):
    checks = batch_checks()
    if change == 'duplicate':checks[1] = checks[0]
    elif change == 'foreign':checks[1]['table_key'] = 'other.schema.table'
    elif change == 'large':checks *= 17
    elif change == 'fields':checks[1]['private_text'] = 'not admitted'
    run = Mock()
    monkeypatch.setattr(central_sql, '_run_central_command', run)
    with pytest.raises(ValueError):
        central_sql.confirm_central_descriptions(metadata_binding(), object(), checks, Mock())
    run.assert_not_called()


def test_cancelling_hash_batch_drains_its_worker_even_when_cancelled_again(monkeypatch):
    from threading import Event
    entered, release, cleaned = Event(), Event(), Event()
    def run(config, signer, checks, check):
        entered.set()
        try:
            assert release.wait(3)
            check()
            raise AssertionError('Cancelled read continued')
        finally:
            cleaned.set()
    monkeypatch.setattr(central_sql, 'confirm_central_descriptions', run)
    transport = SimpleNamespace(signer=object(), access_mode='CENTRAL_METADATA', check_identity=Mock())
    async def cancel():
        task = asyncio.create_task(central_sql.native_descriptions_confirmation({}, batch_checks(), transport=transport))
        assert await asyncio.to_thread(entered.wait, 3)
        for _ in range(2):
            task.cancel()
            for _ in range(3):await asyncio.sleep(0)
            assert not task.done() and not cleaned.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert cleaned.is_set()
    asyncio.run(cancel())
