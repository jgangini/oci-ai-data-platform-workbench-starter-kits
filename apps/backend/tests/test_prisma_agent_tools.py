"""Offline checks of the registered tools; no live AIDP, OCI model or database."""
import asyncio
import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from app.prisma.agent import PROMPT, PrismaAgent
from app.prisma.runtime_secrets import identity_hash, runtime_auth, shared_credential, signer, values


def runtime(monkeypatch):
    credentials = {"region": "us-chicago-1", "compartment_id": "test-compartment", "model_id": "test-model",
        "tenancy": "test-tenancy", "user": "test-user", "fingerprint": "test-fingerprint", "private_key": "test-key"}
    secret_get = MagicMock(side_effect=lambda *, name, key: credentials[key])
    cursor = MagicMock()
    database = MagicMock()
    database.return_value.__enter__.return_value.cursor.return_value = cursor
    llm, memory = object(), object()
    init_llm = MagicMock(return_value=llm)
    native_signer = MagicMock(side_effect=lambda **kwargs: SimpleNamespace(credentials=kwargs))
    monkeypatch.setattr("oci.signer.Signer", native_signer)
    inference_client = MagicMock(side_effect=lambda **kwargs: SimpleNamespace(settings=kwargs))
    checkpoint = MagicMock(return_value=memory)
    configuration = {"configurable": {"thread_id": "session-42"}, "metadata": {"request_id": "request-7"}}
    pre_invoke = MagicMock(return_value=configuration)
    graph = SimpleNamespace(ainvoke=AsyncMock(return_value={"messages": [{"role": "assistant", "content": "result"}]}))
    create_agent = MagicMock(return_value=graph)
    modules = {
        "aidputils": SimpleNamespace(secrets=SimpleNamespace(get=secret_get)),
        "aidputils.agents.toolkit.agent_helper": SimpleNamespace(GenAIChatInvoker=init_llm,
            GenerativeAiInferenceV2Client=inference_client, pre_invoke_setup=pre_invoke),
        "langchain_core.tools": SimpleNamespace(tool=lambda function: function),
        "langgraph.prebuilt": SimpleNamespace(create_react_agent=create_agent),
        "prisma.runtime_secrets": SimpleNamespace(database_connection=database, signer=signer, values=values),
        "oracle_memory_clients.client": SimpleNamespace(AsyncProxyCheckpointClient=checkpoint),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return SimpleNamespace(agent=PrismaAgent(), cursor=cursor, database=database, secret_get=secret_get,
        credentials=credentials, llm=llm, init_llm=init_llm, native_signer=native_signer, inference_client=inference_client,
        memory=memory, checkpoint=checkpoint,
        configuration=configuration, pre_invoke=pre_invoke, graph=graph, create_agent=create_agent)


def test_setup_and_invoke_keep_configured_model_tools_and_session_memory(monkeypatch):
    fake = runtime(monkeypatch)
    monkeypatch.setenv("MEMORY_SERVER_URL", "http://checkpoint.test:21100")
    monkeypatch.setenv("MEMORY_URL", "http://unused.test:21100")
    fake.agent.setup()

    assert fake.secret_get.call_args_list == [call(name="PrismaReaderRuntime", key=key)
        for key in ("region", "compartment_id", "model_id")] + [call(name="PrismaWriterRuntime", key=key)
        for key in ("tenancy", "user", "fingerprint", "private_key")]
    fake.native_signer.assert_called_once_with(tenancy="test-tenancy", user="test-user", fingerprint="test-fingerprint",
        private_key_file_location=None, private_key_content="test-key")
    endpoint = "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com"
    assert fake.inference_client.call_args.kwargs["endpoint"] == endpoint
    assert fake.inference_client.call_count == 1
    client = fake.init_llm.call_args.kwargs["client"]
    assert client.settings == fake.inference_client.call_args.kwargs
    assert client.settings["signer"].credentials == fake.native_signer.call_args.kwargs
    fake.init_llm.assert_called_once_with(provider="generic", model_id="test-model", auth_type="API_KEY",
        compartment_id="test-compartment", service_endpoint=endpoint, client=client, is_stream=True,
        model_kwargs={"temperature": 0, "max_tokens": 2048}, guardrails_config={"policies": []})
    fake.checkpoint.assert_called_once_with(base_url="http://checkpoint.test:21100", agent="prisma_bogota")
    args, kwargs = fake.create_agent.call_args
    assert args[0] is fake.llm
    assert [tool.__name__ for tool in args[1]] == ["consultar_incidentes", "consultar_evidencia", "consultar_sensores"]
    assert kwargs == {"prompt": PROMPT, "checkpointer": fake.memory}

    query = json.dumps({"question": "Compara el sensor y las publicaciones", "context": {"version": "publication-42"}})
    result = asyncio.run(fake.agent.invoke(query, session_id="session-42", request_id="request-7"))
    fake.pre_invoke.assert_called_once_with(session_id="session-42", request_id="request-7")
    fake.graph.ainvoke.assert_awaited_once_with({"messages": [{"role": "user", "content": query}]}, config=fake.configuration)
    assert result is fake.graph.ainvoke.return_value
    fake.database.assert_not_called()


def test_each_agent_owns_its_inference_client_without_cached_remote_state(monkeypatch):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    PrismaAgent().setup()
    clients = [item.kwargs["client"] for item in fake.init_llm.call_args_list]
    signers = [item.kwargs["signer"] for item in fake.inference_client.call_args_list]
    assert len(clients) == len(signers) == 2
    assert clients[0] is not clients[1] and signers[0] is not signers[1]


def test_explicit_model_and_shared_credential_replace_stale_reader_model_only(monkeypatch):
    fake = runtime(monkeypatch)
    config = {"region": "us-chicago-1", "compartment_id": "test-compartment", "model_id": "governance-model",
        "oci_credential_name": "AidpDataGovernanceExtension", "oci_identity_sha256": identity_hash(fake.credentials)}
    monkeypatch.setattr("app.prisma.agent.RUNTIME_CONFIG", config)
    fake.agent.setup()
    assert fake.init_llm.call_args.kwargs["model_id"] == "governance-model"
    assert fake.secret_get.call_args_list == [call(name="AidpDataGovernanceExtension", key=key)
        for key in ("tenancy", "user", "fingerprint", "private_key")]
    tools = {tool.__name__: tool for tool in fake.create_agent.call_args.args[1]}
    fake.cursor.fetchall.return_value = []
    tools["consultar_sensores"]("publication")
    fake.database.assert_called_once_with(fake.secret_get, "PrismaReaderRuntime")


@pytest.mark.parametrize("factory", [signer, runtime_auth])
def test_shared_identity_drift_fails_before_private_key_read_without_fallback(monkeypatch, factory):
    fake = runtime(monkeypatch)
    expected = identity_hash(fake.credentials)
    fake.credentials["user"] = "another-user"
    args = (fake.secret_get,) + (("us-chicago-1",) if factory is runtime_auth else ())
    with pytest.raises(RuntimeError, match="identity mismatch"):
        factory(*args, credential_name="AidpDataGovernanceExtension", expected_identity=expected)
    assert fake.secret_get.call_args_list == [call(name="AidpDataGovernanceExtension", key=key)
        for key in ("tenancy", "user", "fingerprint")]
    fake.native_signer.assert_not_called()


def test_shared_credential_selector_prefers_governance_and_rejects_invalid_preferred():
    writer = {"displayName": "PrismaWriterRuntime", "credentialType": "SECRET_TOKEN", "lifeCycleState": "ACTIVE", "key": "writer"}
    governance = {"displayName": "AidpDataGovernanceExtension", "type": "SECRET_TOKEN", "lifecycleState": "ACTIVE", "key": "governance"}
    assert shared_credential([writer, governance]) is governance
    assert shared_credential([writer]) is writer
    assert shared_credential([]) is None
    for invalid in ([writer, governance, dict(governance)], [writer, {**governance, "type": "VAULT_REFERENCE"}],
                    [writer, {**governance, "lifecycleState": "FAILED"}]):
        with pytest.raises(RuntimeError):
            shared_credential(invalid)


@pytest.mark.parametrize("failure", ["missing_credential", "invalid_signer", "client_initialization"])
def test_setup_fails_closed_when_existing_oci_authentication_is_unavailable(monkeypatch, failure):
    fake = runtime(monkeypatch)
    if failure == "missing_credential":
        fake.credentials["private_key"] = ""
    elif failure == "invalid_signer":
        fake.native_signer.side_effect = RuntimeError("Invalid OCI signer")
    else:
        fake.inference_client.side_effect = RuntimeError("OCI client unavailable")
    with pytest.raises(RuntimeError):
        fake.agent.setup()
    fake.init_llm.assert_not_called()
    fake.checkpoint.assert_not_called()
    fake.create_agent.assert_not_called()
    fake.database.assert_not_called()
    assert fake.agent.agent is None


def test_registered_tools_return_sensor_and_social_records_with_exact_scope(monkeypatch):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    tools = {tool.__name__: tool for tool in fake.create_agent.call_args.args[1]}
    sensor = {"id": "reading-42", "sensor_id": "CO-11-001-rainfall", "sensor_type": "rainfall",
        "locality": "Kennedy", "observed_at": "2026-10-04T14:15:00Z", "value": 35.2, "unit": "mm/h",
        "status": "critical", "lat": 4.6, "lon": -74.15, "mode": "Synthetic", "is_simulated": True}
    incident = {"id": "incident-42", "locality": "Kennedy", "category": "flood", "severity": "high",
        "lat": 4.6, "lon": -74.15, "evidence_ids": ["post-42"], "review_status": "pending", "mode": "Synthetic"}
    evidence = {"id": "post-42", "platform": "x", "text": "Reporte de inundación", "locality": "Kennedy",
        "created_at": "2026-10-04T14:20:00Z", "mode": "Synthetic"}
    fake.cursor.fetchall.side_effect = [[(io.StringIO(json.dumps(sensor)),)], [(json.dumps(incident),)], [(json.dumps(evidence),)]]
    common = {"version": "publication-42", "locality": "Kennedy", "bbox": "-74.2,4.5,-74.1,4.7",
        "date_from": "2026-10-04T09:00:00-05:00", "date_to": "2026-10-04T14:30:00Z"}
    assert tools["consultar_sensores"](**common, sensor_id=sensor["sensor_id"], sensor_type="rainfall") == [sensor]
    assert tools["consultar_incidentes"](**common, incident_id="incident-42", category="flood", severity="high",
        mode="simulation", platform="x") == [incident]
    assert tools["consultar_evidencia"]("publication-42", "post-42") == [evidence]

    sensor_sql, sensor_binds = fake.cursor.execute.call_args_list[0].args
    incident_sql, incident_binds = fake.cursor.execute.call_args_list[1].args
    evidence_sql, evidence_binds = fake.cursor.execute.call_args_list[2].args
    scope = {"version": "publication-42", "locality": "Kennedy", "west": -74.2, "south": 4.5, "east": -74.1, "north": 4.7,
        "date_from": "2026-10-04T14:00:00+00:00", "date_to": "2026-10-04T14:30:00+00:00"}
    assert sensor_binds == {**scope, "sensor_id": sensor["sensor_id"], "sensor_type": "rainfall"}
    assert incident_binds == {**scope, "incident_id": "incident-42", "category": "flood", "severity": "high", "source_mode": "Synthetic", "platform": "x"}
    assert evidence_binds == {"version": "publication-42", "evidence_id": "post-42"}
    assert "ADMIN.PRISMA_V_SENSOR_EVENTS WHERE version=:version" in sensor_sql
    assert "ADMIN.PRISMA_V_INCIDENTS i WHERE i.version=:version" in incident_sql
    assert "e.version=i.version AND e.evidence_id=ids.eid" in incident_sql
    assert evidence_sql == "SELECT evidence_json FROM ADMIN.PRISMA_V_EVIDENCE WHERE version=:version AND evidence_id=:evidence_id"
    for sql in (sensor_sql, incident_sql):
        assert "FETCH FIRST 100 ROWS ONLY" in sql and "publication-42" not in sql and "Kennedy" not in sql
    assert fake.database.call_args_list == [call(fake.secret_get, "PrismaReaderRuntime")] * 3
    assert fake.database.return_value.__exit__.call_count == 3


@pytest.mark.parametrize("name,filters", [
    ("consultar_sensores", {"sensor_id": "station-42"}),
    ("consultar_incidentes", {"incident_id": "incident-42"}),
    ("consultar_evidencia", {"evidence_id": "post-42"}),
])
def test_registered_tools_distinguish_empty_results_from_database_errors(monkeypatch, name, filters):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    tool = next(tool for tool in fake.create_agent.call_args.args[1] if tool.__name__ == name)
    fake.cursor.fetchall.return_value = []
    assert tool(version="publication-42", **filters) == []

    failure = RuntimeError("ADB query unavailable")
    fake.cursor.execute.side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        tool(version="publication-42", **filters)
    assert caught.value is failure
    assert fake.cursor.fetchall.call_count == 1
    assert fake.database.return_value.__exit__.call_args.args[:2] == (RuntimeError, failure)


def test_setup_does_not_replace_missing_credentials_or_failed_memory(monkeypatch):
    fake = runtime(monkeypatch)
    fake.credentials["model_id"] = ""
    with pytest.raises(RuntimeError, match="runtime credential incomplete"):
        fake.agent.setup()
    fake.init_llm.assert_not_called()
    fake.create_agent.assert_not_called()

    fake.credentials["model_id"] = "test-model"
    fake.checkpoint.side_effect = RuntimeError("Checkpoint unavailable")
    with pytest.raises(RuntimeError, match="Checkpoint unavailable"):
        fake.agent.setup()
    fake.create_agent.assert_not_called()
    assert fake.agent.agent is None
