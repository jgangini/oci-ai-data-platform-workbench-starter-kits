"""Offline checks of the registered tools; no live AIDP, OCI model or database."""
import asyncio
import io
import inspect
import json
import sys
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID

import pytest
from pydantic import ValidationError, create_model

from app.territorial.agent import PROMPT, REPLY_SCHEMA, TerritorialAgent, evidence_reply
from app.territorial.core import CATEGORY_NAMES, SEVERITIES
from app.territorial.runtime_secrets import identity_hash, runtime_auth, shared_credential, signer, values
from test_territorial_agent import incident_database, sqlite_rows


def runtime(monkeypatch):
    credentials = {"region": "us-chicago-1", "compartment_id": "test-compartment", "model_id": "test-model",
        "tenancy": "test-tenancy", "user": "test-user", "fingerprint": "test-fingerprint", "private_key": "test-key"}
    monkeypatch.setattr("app.territorial.agent.RUNTIME_CONFIG", {
        **{key: credentials[key] for key in ("region", "compartment_id", "model_id")},
        "oci_credential_name": "AidpRuntime", "oci_identity_sha256": identity_hash(credentials),
        "catalog": "oci_medallion", "gold_query_compute_id": "gold-query-compute"})
    secret_get = MagicMock(side_effect=lambda *, name, key: credentials[key])
    cursor = MagicMock()
    database = MagicMock()
    database.return_value.__enter__.return_value.cursor.return_value = cursor
    def read_gold(config, sql, binds):
        cursor.execute(sql, binds)
        return [json.loads(row[0].read() if hasattr(row[0], "read") else row[0]) for row in cursor.fetchall()]
    gold_query = MagicMock(side_effect=read_gold)
    formatter = SimpleNamespace(ainvoke=AsyncMock(return_value={"incident_refs": [],
        "answer": "No matching rows", "version": "v1",
        "evidence_ids": [], "sensor_evidence_ids": [], "actions": []}))
    planner = SimpleNamespace(ainvoke=AsyncMock(return_value={"tool": "finish", "args": {}}))
    memory = object()
    llm = SimpleNamespace(with_structured_output=MagicMock(side_effect=lambda schema, **kwargs:
        planner if "tool" in schema["properties"] else formatter), bind_tools=MagicMock())
    init_llm = MagicMock(return_value=llm)
    native_signer = MagicMock(side_effect=lambda **kwargs: SimpleNamespace(credentials=kwargs))
    monkeypatch.setattr("oci.signer.Signer", native_signer)
    inference_client = MagicMock(side_effect=lambda **kwargs: SimpleNamespace(settings=kwargs))
    checkpoint = MagicMock(return_value=memory)
    configuration = {"configurable": {"thread_id": "session-42"}, "metadata": {"request_id": "request-7"}}
    pre_invoke = MagicMock(return_value=configuration)
    class GraphInvocation(AsyncMock):
        async def _execute_mock_call(self, inputs, *, config):
            state = await super()._execute_mock_call(inputs, config=config)
            current = next((message for message in reversed(state["messages"])
                            if isinstance(message, messages.HumanMessage)), None)
            if current is None or current.id != inputs["messages"][0].id:
                return state
            # Execute the registered final hook before the fake graph's normal checkpoint.
            update = await agent.final_response(state, config)
            saved = [*state["messages"][:-1], *update["messages"]] if update else state["messages"]
            graph.checkpointed = saved
            return {**state, "messages": saved}

    graph = SimpleNamespace(ainvoke=GraphInvocation(return_value={"messages": []}),
        aupdate_state=AsyncMock(), checkpointed=[])
    messages = SimpleNamespace(HumanMessage=type("HumanMessage", (SimpleNamespace,), {}),
        SystemMessage=type("SystemMessage", (SimpleNamespace,), {}),
        AIMessage=type("AIMessage", (SimpleNamespace,), {"tool_calls": []}),
        ToolMessage=type("ToolMessage", (SimpleNamespace,), {"status": "success", "name": None}))
    create_agent = MagicMock(return_value=graph)
    tool_exception = type("ToolException", (Exception,), {})
    tool_node = MagicMock(side_effect=lambda tools, **kwargs: list(tools))
    runnable = MagicMock(side_effect=lambda function: SimpleNamespace(ainvoke=function))
    modules = {
        "aidputils": SimpleNamespace(secrets=SimpleNamespace(get=secret_get)),
        "aidputils.agents.toolkit.agent_helper": SimpleNamespace(GenAIChatInvoker=init_llm,
            GenerativeAiInferenceV2Client=inference_client, pre_invoke_setup=pre_invoke),
        "langchain_core.tools": SimpleNamespace(tool=lambda function: function, ToolException=tool_exception),
        "langchain_core.messages": messages,
        "langchain_core.runnables": SimpleNamespace(RunnableLambda=runnable),
        "langgraph.prebuilt": SimpleNamespace(create_react_agent=create_agent, ToolNode=tool_node),
        "territorial.runtime_secrets": SimpleNamespace(database_connection=database, signer=signer, values=values),
        "territorial.gold_reader": SimpleNamespace(query=gold_query),
        "oracle_memory_clients.client": SimpleNamespace(AsyncProxyCheckpointClient=checkpoint),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    agent = TerritorialAgent()
    agent.llm = llm
    return SimpleNamespace(agent=agent, formatter=formatter, planner=planner, runnable=runnable,
        cursor=cursor, database=database, gold_query=gold_query, secret_get=secret_get,
        credentials=credentials, llm=llm, init_llm=init_llm, native_signer=native_signer, inference_client=inference_client,
        memory=memory, checkpoint=checkpoint,
        configuration=configuration, pre_invoke=pre_invoke, graph=graph, create_agent=create_agent, messages=messages,
        tool_node=tool_node, tool_exception=tool_exception)


def test_setup_and_invoke_keep_configured_model_tools_and_session_memory(monkeypatch):
    fake = runtime(monkeypatch)
    monkeypatch.setenv("MEMORY_SERVER_URL", "http://checkpoint.test:21100")
    monkeypatch.setenv("MEMORY_URL", "http://unused.test:21100")
    fake.agent.setup()

    assert fake.secret_get.call_args_list == [call(name="AidpRuntime", key=key)
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
        compartment_id="test-compartment", service_endpoint=endpoint, client=client, is_stream=False,
        model_kwargs={"temperature": 0, "max_tokens": 2048}, guardrails_config={"policies": []})
    fake.checkpoint.assert_called_once_with(base_url="http://checkpoint.test:21100", agent="prisma_bogota")
    args, kwargs = fake.create_agent.call_args
    assert callable(args[0])
    assert [tool.__name__ for tool in args[1]] == ["consultar_incidentes", "consultar_evidencia", "consultar_sensores"]
    tools = fake.tool_node.call_args.args[0]
    fake.tool_node.assert_called_once_with(tools, handle_tool_errors=(ValueError, fake.tool_exception))
    assert args[1] == tools and args[1] is not tools
    handled = fake.tool_node.call_args.kwargs["handle_tool_errors"]
    assert isinstance(ValueError("Missing evidence ID"), handled)
    assert isinstance(fake.tool_exception("Invalid tool arguments"), handled)
    assert not isinstance(RuntimeError("ADB query unavailable"), handled)
    fake.llm.bind_tools.assert_not_called()
    assert kwargs == {"prompt": PROMPT, "checkpointer": fake.memory,
        "post_model_hook": fake.agent.final_response, "version": "v2"}
    fake.llm.with_structured_output.assert_called_once()
    planner_schema = fake.llm.with_structured_output.call_args.args[0]
    assert fake.llm.with_structured_output.call_args.kwargs == {"method": "json_schema"}
    assert planner_schema["properties"]["tool"]["enum"] == [
        "consultar_incidentes", "consultar_evidencia", "consultar_sensores", "finish"]
    assert "version" not in planner_schema["properties"]["args"]["properties"]
    assert fake.agent.llm is fake.llm

    query = json.dumps({"question": "Compara el sensor y las publicaciones", "context": {"version": "publication-42"}})
    def grounded_result(inputs, *, config):
        return {"messages": [*inputs["messages"], fake.messages.AIMessage(tool_calls=[
            {"id": "call-42", "name": "consultar_sensores", "args": {"version": "publication-42"}}]),
            fake.messages.ToolMessage(tool_call_id="call-42", name="consultar_sensores", content="[]"),
            fake.messages.AIMessage(content="result")]}
    fake.graph.ainvoke.side_effect = grounded_result
    fake.formatter.ainvoke.return_value["version"] = "publication-42"
    result = asyncio.run(fake.agent.invoke(query, session_id="session-42", request_id="request-7"))
    fake.pre_invoke.assert_called_once_with(session_id="session-42", request_id="request-7")
    assert fake.graph.ainvoke.await_count == 1
    turn = fake.graph.ainvoke.call_args.args[0]["messages"][0]
    assert isinstance(turn, fake.messages.HumanMessage) and str(UUID(turn.id)) == turn.id
    assert turn.content == query and fake.graph.ainvoke.call_args.kwargs == {"config": fake.configuration}
    assert fake.configuration["recursion_limit"] == 24
    assert list(result) == ["messages"] and len(result["messages"]) == 1
    assert isinstance(result["messages"][0], fake.messages.AIMessage)
    assert json.loads(result["messages"][0].content) == {key: value for key, value in fake.formatter.ainvoke.return_value.items()
        if key != "incident_refs"}
    assert fake.llm.with_structured_output.call_count == 2
    schema = fake.llm.with_structured_output.call_args.args[0]
    assert fake.llm.with_structured_output.call_args.kwargs == {"method": "json_schema"}
    assert schema["properties"]["version"] == {"type": "string", "enum": ["publication-42"]}
    for field in ("evidence_ids", "sensor_evidence_ids"):
        assert field not in schema["properties"] and field not in schema["required"]
    fake.formatter.ainvoke.assert_awaited_once()
    formatting_messages = fake.formatter.ainvoke.call_args.args[0]
    assert isinstance(formatting_messages[0], fake.messages.SystemMessage)
    assert formatting_messages[0].content != PROMPT
    assert "queries" in formatting_messages[0].content
    assert isinstance(formatting_messages[1], fake.messages.HumanMessage)
    assert json.loads(formatting_messages[1].content) == {"request": json.loads(query), "independence_status": "independence_not_established", "queries": [
        {"tool": "consultar_sensores", "filters": {"version": "publication-42"}, "rows": []}]}
    assert fake.formatter.ainvoke.call_args.kwargs == {"config": fake.configuration}
    assert fake.graph.checkpointed[-1] is result["messages"][0]
    fake.graph.aupdate_state.assert_not_awaited()
    fake.database.assert_not_called()


def test_final_hook_passes_tool_calls_to_toolnode_without_formatting_or_manual_memory_write(monkeypatch):
    fake = runtime(monkeypatch)
    messages = [fake.messages.HumanMessage(id="turn", content='{"context":{"version":"v1"}}'),
        fake.messages.AIMessage(content="", tool_calls=[
            {"id": "query-1", "name": "consultar_incidentes", "args": {"version": "v1"}}])]
    assert asyncio.run(fake.agent.final_response({"messages": messages}, fake.configuration)) == {}
    fake.formatter.ainvoke.assert_not_awaited()
    fake.graph.aupdate_state.assert_not_awaited()


@pytest.mark.parametrize("context,names", [
    ({"sensor_id": "sensor-1"}, ["consultar_sensores"]),
    ({"incident_id": "incident-1"}, ["consultar_incidentes", "consultar_evidencia"]),
    ({"sensor_id": "sensor-1", "incident_id": "incident-1"}, ["consultar_incidentes", "consultar_evidencia", "consultar_sensores"]),
])
def test_planner_queries_both_selected_resources_before_it_can_finish(monkeypatch, context, names):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    messages = fake.messages
    state = [messages.HumanMessage(id="old", content='{"question":"Earlier","context":{"version":"v0"}}'),
        messages.AIMessage(content="Old answer"), messages.HumanMessage(id="current",
            content=json.dumps({"question": "Compare", "context": {"version": "v1", **context}}))]
    call_ids = []
    for name in names:
        reply = planned(fake, state)
        assert isinstance(reply, messages.AIMessage) and len(reply.tool_calls) == 1
        query = reply.tool_calls[0]
        key = "sensor_id" if name == "consultar_sensores" else "incident_id"
        assert query["name"] == name and query["args"] == {key: context[key], "version": "v1"}
        assert str(UUID(query["id"])) == query["id"]
        call_ids.append(query["id"])
        fake.planner.ainvoke.assert_not_awaited()
        state.extend([reply, messages.ToolMessage(tool_call_id=query["id"], name=name, content=[])])
    assert len(set(call_ids)) == len(names)
    assert planned(fake, state).tool_calls == []
    fake.planner.ainvoke.assert_awaited_once()
    fake.llm.bind_tools.assert_not_called()


def planned(fake, messages):
    adapter = fake.create_agent.call_args.args[0]({"messages": messages}, None)
    return asyncio.run(adapter.ainvoke(messages, config=fake.configuration))


def completed_query(fake, name="consultar_incidentes", args=None, rows=None, call_id="query-1"):
    return [fake.messages.AIMessage(content="", tool_calls=[
        {"id": call_id, "name": name, "args": args or {"version": "v1"}}]),
        fake.messages.ToolMessage(tool_call_id=call_id, name=name, content=json.dumps(rows or []))]


@pytest.mark.parametrize("narrowed", [{"evidence_id": "facebook-only"}, {"platform": "facebook"}])
def test_selected_incident_requires_diverse_evidence_query_not_a_previous_narrow_sample(monkeypatch, narrowed):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    request = {"question": "Compara dos redes", "context": {"version": "v1", "incident_id": "incident-1"}}
    state = [fake.messages.HumanMessage(content=json.dumps(request))]
    scope = {"version": "v1", "incident_id": "incident-1"}
    state += completed_query(fake, args=scope)
    state += completed_query(fake, "consultar_evidencia", {**scope, **narrowed},
        [{"id": "facebook-only", "platform": "facebook"}], "narrow")
    query = planned(fake, state).tool_calls[0]
    assert query["name"] == "consultar_evidencia" and query["args"] == scope
    fake.planner.ainvoke.assert_not_awaited()
    state += completed_query(fake, "consultar_evidencia", scope,
        [{"id": "facebook-only", "platform": "facebook"}, {"id": "x-other", "platform": "x"}], "diverse")
    assert planned(fake, state).tool_calls == []


@pytest.mark.parametrize("name,expected", [
    ("consultar_incidentes", {"incident_id": "selected", "country": "Colombia", "city": "Bogotá", "locality": "Bosa",
        "mode": "real", "severity": "high", "platform": "x", "date_from": "2026-10-05T00:00:00Z", "bbox": "-75,4,-73,6"}),
    ("consultar_evidencia", {"incident_id": "selected", "platform": "x"}),
    ("consultar_sensores", {"sensor_id": "selected-sensor", "locality": "Bosa",
        "date_from": "2026-10-05T00:00:00Z", "bbox": "-75,4,-73,6"}),
])
def test_server_applies_version_and_context_filters_over_planner_arguments(monkeypatch, name, expected):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    filters = {"country": "Colombia", "city": "Bogotá", "locality": "Bosa", "mode": "real", "severity": "high",
        "platform": "x", "date_from": "2026-10-05T00:00:00Z", "bbox": "-75,4,-73,6"}
    request = {"question": "Read selected", "context": {"version": "v1", "incident_id": "selected",
        "sensor_id": "selected-sensor", "filters": filters}}
    state = [fake.messages.HumanMessage(content=json.dumps(request))]
    # Mandatory reads have completed; test the subsequent model-proposed query as well.
    for index, tool in enumerate(fake.create_agent.call_args.args[1]):
        scope = {key: value for key, value in filters.items() if key in inspect.signature(tool).parameters}
        selected = {"sensor_id": "selected-sensor"} if tool.__name__ == "consultar_sensores" else {"incident_id": "selected"}
        state += completed_query(fake, tool.__name__, {"version": "v1", **scope, **selected}, call_id=f"query-{index}")
    args = {key: "model-tried-another-value" for key in expected}
    # Add a valid optional field so this is a new query rather than a successful exact repeat.
    field = "evidence_id" if name == "consultar_evidencia" else "sensor_type" if name == "consultar_sensores" else "category"
    args[field] = "post-1" if name == "consultar_evidencia" else "rainfall" if name == "consultar_sensores" else "lluvia"
    expected = {**expected, field: args[field]}
    fake.planner.ainvoke.return_value = {"tool": name, "args": args}
    before = json.dumps(request, sort_keys=True)
    reply = planned(fake, state)
    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0]["name"] == name
    assert reply.tool_calls[0]["args"] == {**expected, "version": "v1"}
    assert json.dumps(request, sort_keys=True) == before
    assert fake.planner.ainvoke.return_value == {"tool": name, "args": args}


def test_planner_followup_uses_memory_but_requires_fresh_current_version_query(monkeypatch):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    state = [fake.messages.HumanMessage(content='{"question":"Previous","context":{"version":"v0"}}')]
    state += completed_query(fake, args={"version": "v0"}, rows=[{"id": "old-event"}])
    state += [fake.messages.AIMessage(content='{"answer":"Previous grounded answer"}'),
        fake.messages.HumanMessage(content='{"question":"¿Y ahora?","context":{"version":"v1"}}')]
    with pytest.raises(ValueError, match="completed query"):
        planned(fake, state)
    fake.planner.ainvoke.return_value = {"tool": "consultar_incidentes", "args": {"country": "Colombia"}}
    reply = planned(fake, state)
    assert reply.tool_calls[0]["args"] == {"country": "Colombia", "version": "v1"}
    planning_messages = fake.planner.ainvoke.call_args.args[0]
    assert [type(message) for message in planning_messages] == [fake.messages.SystemMessage, fake.messages.HumanMessage]
    body = json.loads(planning_messages[1].content)
    assert body["queries"] == [] and body["request"]["question"] == "¿Y ahora?"
    assert [message["role"] for message in body["history"]] == ["user", "assistant"]
    assert body["history"][-1]["content"] == '{"answer":"Previous grounded answer"}'
    assert "old-event" not in planning_messages[1].content
    assert fake.planner.ainvoke.call_args.kwargs == {"config": fake.configuration}


@pytest.mark.parametrize("plan", [None, [], {"tool": "finish"}, {"tool": "finish", "args": {}, "extra": "ignored?"},
    {"tool": ["consultar_incidentes", "consultar_sensores"], "args": {}},
    {"tool": "not-a-tool", "args": {}}, {"tool": "consultar_incidentes", "args": []},
    {"tool": "consultar_incidentes", "args": {"version": "v0"}},
    {"tool": "consultar_incidentes", "args": {"locality": None}},
    {"tool": "consultar_incidentes", "args": {"locality": "x" * 201}},
    {"tool": "consultar_incidentes", "args": {"sql": "SELECT secrets"}},
    {"tool": "finish", "args": {"country": "Colombia"}},
])
def test_planner_rejects_malformed_ambiguous_or_out_of_scope_plans(monkeypatch, plan):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    fake.planner.ainvoke.return_value = plan
    state = [fake.messages.HumanMessage(content='{"question":"Events","context":{"version":"v1"}}')]
    with pytest.raises(ValueError):
        planned(fake, state)
    fake.database.assert_not_called()


@pytest.mark.parametrize("mode", ["", "all"])
def test_planner_all_modes_does_not_filter_out_synthetic_and_real_rows(monkeypatch, mode):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    state = [fake.messages.HumanMessage(content='{"question":"Eventos en Colombia","context":{"version":"v1"}}')]
    fake.planner.ainvoke.return_value = {"tool": "consultar_incidentes", "args": {"country": "Colombia", "mode": mode}}
    query = planned(fake, state).tool_calls[0]
    assert query["args"] == {"country": "Colombia", "version": "v1"}
    tools = {tool.__name__: tool for tool in fake.create_agent.call_args.args[1]}
    fake.cursor.fetchall.return_value = []
    tools[query["name"]](**query["args"])
    assert fake.cursor.execute.call_args.args[1]["source_mode"] is None


def test_planner_evidence_discards_only_inapplicable_union_fields_and_keeps_selected_scope(monkeypatch):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    request = {"question": "Compara sensor y publicaciones", "context": {"version": "v1",
        "incident_id": "selected", "sensor_id": "selected-sensor", "filters": {"platform": "x"}}}
    state = [fake.messages.HumanMessage(content=json.dumps(request))]
    state += completed_query(fake, args={"version": "v1", "incident_id": "selected", "platform": "x"})
    state += completed_query(fake, "consultar_evidencia", {"version": "v1", "incident_id": "selected", "platform": "x"}, call_id="evidence")
    state += completed_query(fake, "consultar_sensores", {"version": "v1", "sensor_id": "selected-sensor"}, call_id="sensor")
    args = {key: "not-applicable" for key in ("bbox", "category", "city", "country", "date_from", "date_to",
        "locality", "mode", "sensor_id", "sensor_type", "severity")}
    args.update(evidence_id="post-1", incident_id="model-tried-another", platform="facebook")
    fake.planner.ainvoke.return_value = {"tool": "consultar_evidencia", "args": args}
    query = planned(fake, state).tool_calls[0]
    assert query["name"] == "consultar_evidencia"
    assert query["args"] == {"evidence_id": "post-1", "incident_id": "selected", "platform": "x", "version": "v1"}
    assert fake.planner.ainvoke.return_value["args"] == args


def test_planner_finish_ignores_union_values_only_after_successful_current_query(monkeypatch):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    fake.planner.ainvoke.return_value = {"tool": "finish", "args": {"category": "inundacion", "country": "Colombia",
        "incident_id": "selected", "sensor_id": "selected-sensor", "severity": "high"}}
    state = [fake.messages.HumanMessage(content='{"question":"Borrador","context":{"version":"v1"}}')]
    with pytest.raises(ValueError, match="completed query"):
        planned(fake, state)
    state += completed_query(fake)
    assert planned(fake, state).tool_calls == []


@pytest.mark.parametrize("failure", ["error", "unlinked", "wrong_name", "wrong_version", "malformed", "non_records"])
def test_planner_does_not_continue_after_failed_or_invalid_current_query(monkeypatch, failure):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    state = [fake.messages.HumanMessage(content='{"question":"Events","context":{"version":"v1"}}')]
    tail = completed_query(fake)
    if failure == "error":
        tail[1].status, tail[1].content = "error", "Native query unavailable"
    elif failure == "unlinked":
        tail[1].tool_call_id = "another-call"
    elif failure == "wrong_name":
        tail[1].name = "consultar_sensores"
    elif failure == "wrong_version":
        tail[0].tool_calls[0]["args"]["version"] = "v0"
    else:
        tail[1].content = "not-json" if failure == "malformed" else "[null]"
    with pytest.raises((RuntimeError, json.JSONDecodeError)):
        planned(fake, state + tail)
    fake.planner.ainvoke.assert_not_awaited()
    fake.formatter.ainvoke.assert_not_awaited()


def test_planner_bounds_queries_and_finishes_completed_duplicates_without_retrying_failures(monkeypatch):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    state = [fake.messages.HumanMessage(content='{"question":"Events","context":{"version":"v1"}}')]
    state += completed_query(fake, args={"country": "Colombia", "version": "v1"})
    fake.planner.ainvoke.return_value = {"tool": "consultar_incidentes", "args": {"country": "Colombia", "city": ""}}
    assert planned(fake, state).tool_calls == []
    fake.database.assert_not_called()
    for index in range(2, 7):
        state += completed_query(fake, args={"version": "v1", "incident_id": f"event-{index}"}, call_id=f"query-{index}")
    fake.planner.ainvoke.return_value = {"tool": "consultar_incidentes", "args": {"incident_id": "event-6"}}
    assert planned(fake, state).tool_calls == []
    fake.planner.ainvoke.return_value = {"tool": "consultar_sensores", "args": {}}
    with pytest.raises(RuntimeError, match="six"):
        planned(fake, state)
    fake.planner.ainvoke.return_value = {"tool": "finish", "args": {}}
    assert planned(fake, state).tool_calls == []
    fake.planner.ainvoke.side_effect = RuntimeError("Planner unavailable")
    before = fake.planner.ainvoke.await_count
    with pytest.raises(RuntimeError, match="Planner unavailable"):
        planned(fake, state)
    assert fake.planner.ainvoke.await_count == before + 1


@pytest.mark.parametrize("failure", ["no_tools", "history_only", "tool_error", "wrong_version", "unlinked_result",
    "wrong_name", "unknown_tool", "missing_turn", "later_turn", "success_then_error"])
def test_invoke_requires_a_linked_successful_tool_in_the_current_publication_turn(monkeypatch, failure):
    fake = runtime(monkeypatch)
    fake.agent.agent = fake.graph
    messages = fake.messages

    def response(inputs, *, config):
        turn = inputs["messages"][0]
        history = [messages.HumanMessage(id="earlier", content="Earlier question"),
            messages.AIMessage(tool_calls=[{"id": "old-call", "name": "consultar_incidentes", "args": {"version": "v1"}}]),
            messages.ToolMessage(tool_call_id="old-call", name="consultar_incidentes", content="[]")]
        call = {"id": "current-call", "name": "consultar_incidentes", "args": {"version": "v1"}}
        tool = messages.ToolMessage(tool_call_id="current-call", name="consultar_incidentes", content="[]")
        if failure == "wrong_version":
            call["args"]["version"] = "v0"
        elif failure in {"tool_error", "success_then_error"}:
            tool.status, tool.content = "error", "Database unavailable"
        elif failure == "unlinked_result":
            tool.tool_call_id = "old-call"
        elif failure == "wrong_name":
            tool.name = "consultar_sensores"
        elif failure == "unknown_tool":
            call["name"] = tool.name = "unregistered_tool"
        tail = [] if failure in {"no_tools", "history_only"} else [messages.AIMessage(tool_calls=[call]), tool]
        if failure == "success_then_error":
            tail = completed_query(fake, call_id="successful") + tail
        return {"messages": [*([] if failure == "no_tools" else history),
            *([] if failure == "missing_turn" else [turn]), *tail,
            *([messages.HumanMessage(id="another-turn", content="Concurrent question")] if failure == "later_turn" else []),
            messages.AIMessage(content='{"answer":"No events","version":"v1","evidence_ids":[]}')]}

    fake.graph.ainvoke.side_effect = response
    with pytest.raises(RuntimeError, match="current question|requested publication|current query failed"):
        asyncio.run(fake.agent.invoke(json.dumps({"question": "Eventos en Colombia", "context": {"version": "v1"}})))
    assert fake.graph.ainvoke.await_count == 1
    fake.llm.with_structured_output.assert_not_called()
    fake.formatter.ainvoke.assert_not_awaited()
    fake.graph.aupdate_state.assert_not_awaited()


@pytest.mark.parametrize("name", ["consultar_incidentes", "consultar_evidencia", "consultar_sensores"])
@pytest.mark.parametrize("content", ["[]", []], ids=["json", "native-list"])
def test_invoke_accepts_empty_sql_results_and_uses_a_new_id_for_each_followup(monkeypatch, name, content):
    fake = runtime(monkeypatch)
    fake.agent.agent = fake.graph
    messages = fake.messages
    history = []

    def response(inputs, *, config):
        turn = inputs["messages"][0]
        history.extend([turn, messages.AIMessage(tool_calls=[{"id": turn.id, "name": name, "args": {"version": "v1"}}]),
            messages.ToolMessage(tool_call_id=turn.id, name=name, content=content), messages.AIMessage(content="No matching rows")])
        return {"messages": list(history)}

    fake.graph.ainvoke.side_effect = response
    query = json.dumps({"question": "¿Y ahora?", "context": {"version": "v1"}})
    for _ in range(2):
        result = asyncio.run(fake.agent.invoke(query))
        assert len(result["messages"]) == 1
        assert json.loads(result["messages"][0].content) == {key: value for key, value in fake.formatter.ainvoke.return_value.items()
            if key != "incident_refs"}
    turns = [item for item in history if isinstance(item, messages.HumanMessage)]
    assert len({item.id for item in turns}) == 2
    assert all(item.content == query for item in turns)
    assert fake.formatter.ainvoke.await_count == 2
    assert fake.llm.with_structured_output.call_count == 2
    schemas = [item.args[0] for item in fake.llm.with_structured_output.call_args_list]
    assert schemas[0] is not schemas[1]
    assert all(field not in schema["properties"] and field not in schema["required"]
        for schema in schemas for field in ("evidence_ids", "sensor_evidence_ids"))
    assert all(json.loads(item.args[0][1].content)["queries"] == [
        {"tool": name, "filters": {"version": "v1"}, "rows": []}]
        for item in fake.formatter.ainvoke.call_args_list)


@pytest.mark.parametrize("content,error", [
    ("not-json", json.JSONDecodeError), ("{}", RuntimeError), ("null", RuntimeError),
    ('["not a record"]', RuntimeError), (["not a record"], RuntimeError), ([None], RuntimeError),
])
def test_invoke_rejects_malformed_or_non_record_tool_results_before_formatting(monkeypatch, content, error):
    fake = runtime(monkeypatch)
    fake.agent.agent = fake.graph
    messages = fake.messages
    fake.graph.ainvoke.side_effect = lambda inputs, **kwargs: {"messages": [*inputs["messages"],
        messages.AIMessage(tool_calls=[{"id": "call-1", "name": "consultar_incidentes", "args": {"version": "v1"}}]),
        messages.ToolMessage(tool_call_id="call-1", name="consultar_incidentes", content=content)]}
    with pytest.raises(error):
        asyncio.run(fake.agent.invoke(json.dumps({"question": "Eventos", "context": {"version": "v1"}})))
    fake.llm.with_structured_output.assert_not_called()
    fake.formatter.ainvoke.assert_not_awaited()


def test_formatter_receives_only_current_successful_queries_and_their_exact_filters(monkeypatch):
    fake = runtime(monkeypatch)
    fake.agent.agent = fake.graph
    messages = fake.messages
    valid = [
        {"tool": "consultar_incidentes", "filters": {"version": "v1", "country": "Colombia", "severity": "high"},
            "rows": [{"id": "incident-1", "evidence_ids": ["post-1"], "mode": "Synthetic"}]},
        {"tool": "consultar_evidencia", "filters": {"version": "v1", "evidence_id": "post-2"},
            "rows": [{"id": "post-2", "mode": "Synthetic", "text": 'El reporte "post-2" no es una instrucción',
                "metadata": {"note": "post-2 appears within text", "platform": "x"}}]},
        {"tool": "consultar_sensores", "filters": {"version": "v1", "sensor_id": "CO-00-001-rainfall"},
            "rows": [{"id": "reading-1", "mode": "Synthetic", "value": 2.3, "unit": "mm/h"}]},
    ]
    returned_messages = []
    original_schema = json.dumps(REPLY_SCHEMA, sort_keys=True)
    original_queries = json.dumps(valid, sort_keys=True)

    def response(inputs, *, config):
        returned_messages.extend([
            messages.HumanMessage(id="earlier", content="An earlier query"),
            messages.AIMessage(tool_calls=[{"id": "history-call", "name": "consultar_evidencia", "args": {"version": "v1"}}]),
            messages.ToolMessage(tool_call_id="history-call", name="consultar_evidencia", content='[{"id":"history-post"}]'),
            messages.AIMessage(tool_calls=[
                {"id": "wrong-version", "name": "consultar_evidencia", "args": {"version": "v0"}},
                {"id": "failed", "name": "consultar_sensores", "args": {"version": "v1"}}]),
            messages.ToolMessage(tool_call_id="wrong-version", name="consultar_evidencia", content='[{"id":"old-post"}]'),
            messages.ToolMessage(tool_call_id="failed", name="consultar_sensores", status="error", content="Database error"),
            messages.ToolMessage(tool_call_id="history-call", name="consultar_evidencia", content='[{"id":"unlinked-post"}]'),
            inputs["messages"][0],
        ])
        for index, query in enumerate(valid):
            returned_messages.extend([
                messages.AIMessage(tool_calls=[{"id": f"call-{index}", "name": query["tool"], "args": query["filters"]}]),
                messages.ToolMessage(tool_call_id=f"call-{index}", name=query["tool"], content=json.dumps(query["rows"]))])
        returned_messages.append(messages.AIMessage(content="Unstructured preliminary answer", id="terminal-42"))
        return {"messages": returned_messages}

    fake.graph.ainvoke.side_effect = response
    fake.formatter.ainvoke.return_value = {"incident_refs": [],
        "answer": "Datos Synthetic de Colombia", "version": "v1",
        "evidence_ids": ["E1", "E2"], "sensor_evidence_ids": ["S1"], "actions": []}
    original_formatter_reply = json.dumps(fake.formatter.ainvoke.return_value, sort_keys=True)
    request = {"question": "Compara eventos críticos de Colombia con el sensor", "context": {"version": "v1"}}
    result = asyncio.run(fake.agent.invoke(json.dumps(request)))
    formatted = json.loads(fake.formatter.ainvoke.call_args.args[0][1].content)
    assert formatted["request"] == request
    assert formatted["queries"] == [
        {**valid[0], "rows": [{"id": "incident-1", "incident_ref": "I1", "evidence_ids": ["E1"], "mode": "Synthetic"}]},
        {**valid[1], "filters": {"version": "v1", "evidence_id": "E2"}, "rows": [{**valid[1]["rows"][0], "id": "E2"}]},
        {**valid[2], "rows": [{"id": "S1", "mode": "Synthetic", "value": 2.3, "unit": "mm/h"}]},
    ]
    assert len(result["messages"]) == 1 and result["messages"] is not returned_messages
    actual = json.loads(result["messages"][0].content)
    assert actual["evidence_ids"] == ["post-2"] and actual["sensor_evidence_ids"] == ["reading-1"]
    assert valid[1]["rows"][0]["text"] in actual["answer"] and "history-post" not in actual["answer"]
    assert returned_messages[-1].content == "Unstructured preliminary answer"
    assert result["messages"][0].id == "terminal-42"
    assert fake.graph.checkpointed[-1] is result["messages"][0]
    fake.graph.aupdate_state.assert_not_awaited()
    schema = fake.llm.with_structured_output.call_args.args[0]
    assert schema["properties"]["version"] == {"type": "string", "enum": ["v1"]}
    assert schema["properties"]["evidence_ids"] == {"type": "array", "maxItems": 2,
        "items": {"type": "string", "enum": ["E1", "E2"]}}
    assert schema["properties"]["sensor_evidence_ids"] == {"type": "array", "maxItems": 1,
        "items": {"type": "string", "enum": ["S1"]}}
    assert json.dumps(REPLY_SCHEMA, sort_keys=True) == original_schema
    assert json.dumps(valid, sort_keys=True) == original_queries
    assert json.dumps(fake.formatter.ainvoke.return_value, sort_keys=True) == original_formatter_reply


@pytest.mark.parametrize("body,expected", [
    ("Inundación (E1, E2). Lectura [S1].", "Inundación. Lectura."),
    ("La publicación de Facebook, con ID E1, fue creada a las 12:00 UTC.",
     "La publicación de Facebook fue creada a las 12:00 UTC."),
    ("E1 y E2 coinciden; S1 marca 3.92 m. Código E10 y modelo E1X.",
     "la evidencia citada y la evidencia citada coinciden; la lectura citada marca 3.92 m. Código E10 y modelo E1X."),
    ("Sin anotaciones. Medición 3.92 m, warning.", "Sin anotaciones. Medición 3.92 m, warning."),
])
def test_formatter_hides_only_known_body_aliases_and_preserves_exact_citations_and_facts(monkeypatch, body, expected):
    fake = runtime(monkeypatch)
    scope = {"version": "v1"}
    state = [fake.messages.HumanMessage(content=json.dumps({"question": "Compara", "context": scope}))]
    state += completed_query(fake, "consultar_incidentes", scope, [{"id": "incident-1", "evidence_ids": ["post-1", "post-2"]}])
    state += completed_query(fake, "consultar_sensores", scope, [{"id": "reading-1"}], "sensor")
    state.append(fake.messages.AIMessage(content="Done", id="last"))
    reply = {"answer": body, "version": "v1", "evidence_ids": ["E2", "E1"], "sensor_evidence_ids": ["S1"], "actions": []}
    fake.formatter.ainvoke.return_value = {**reply, "incident_refs": []}
    result = asyncio.run(fake.agent.final_response({"messages": state}, fake.configuration))
    assert json.loads(result["messages"][0].content) == {**reply, "answer": expected,
        "evidence_ids": ["post-2", "post-1"], "sensor_evidence_ids": ["reading-1"]}
    assert fake.formatter.ainvoke.return_value == {**reply, "incident_refs": []}
    instructions = fake.formatter.ainvoke.call_args.args[0][0].content
    assert "confianza de clasificación" in instructions and "nunca probabilidad del incidente real" in instructions
    assert "agrupaciones heurísticas de reportes" in instructions and "independencia no está comprobada" in instructions
    assert "ubicación del REPORTE social" in instructions and "no atribuyas imprecisión al sensor" in instructions


def test_formatter_context_labels_classification_groups_and_association_without_changing_values(monkeypatch):
    fake = runtime(monkeypatch)
    incident = {"id": "incident-1", "evidence_ids": [], "confidence": 0.9, "independent_source_count": 25,
        "corroboration_score": 100, "review_status": "pending", "correlation_context": {
            "sensors": {"status": "insufficient_location_precision", "count": 0, "samples": []},
            "social": {"accounts_per_platform": {"facebook": 2, "x": 1}}}}
    sensor = {"id": "reading-1", "sensor_id": "sensor-1", "value": 5.01, "unit": "m", "status": "critical"}
    evidence = {"id": "post-1", "platform": "x", "incident_relations": [{"event_id": "incident-1",
        "post_key": "post-1", "relation": "duplicate", "claim_relation": "contradicts", "duplicate_of": "original-post",
        "explanation": "Exact image SHA-256 matches an earlier report; caption claims remain separate", "analysis_version": "test"}]}
    before = json.dumps([incident, sensor, evidence], sort_keys=True)
    state = [fake.messages.HumanMessage(content='{"question":"Informe","context":{"version":"v1"}}')]
    state += completed_query(fake, rows=[incident])
    state += completed_query(fake, "consultar_sensores", {"version": "v1"}, [sensor], "sensor")
    state += completed_query(fake, "consultar_evidencia", {"version": "v1", "incident_id": "incident-1"}, [evidence], "evidence")
    fake.formatter.ainvoke.return_value.update(evidence_ids=["E1"])
    state.append(fake.messages.AIMessage(content="Done"))
    asyncio.run(fake.agent.final_response({"messages": state}, fake.configuration))
    source = json.loads(fake.formatter.ainvoke.call_args.args[0][1].content)
    queries = source["queries"]
    row = queries[0]["rows"][0]
    assert row["classification_confidence"] == 0.9 and row["heuristic_report_group_count"] == 25
    assert "confidence" not in row and "independent_source_count" not in row
    assert row["heuristic_corroboration_index"] == 100 and "corroboration_score" not in row
    assert row["review_status"] == "pending" and source["independence_status"] == "independence_not_established"
    assert row["correlation_context"] == {"social": incident["correlation_context"]["social"],
        "report_sensor_association": incident["correlation_context"]["sensors"]}
    assert queries[1]["rows"] == [{**sensor, "id": "S1"}]
    assert queries[2]["rows"] == [{**evidence, "id": "E1", "incident_relations": [
        {**evidence["incident_relations"][0], "post_key": "E1"}]}]
    assert json.dumps([incident, sensor, evidence], sort_keys=True) == before
    assert "report_sensor_association" not in json.loads(state[2].content)[0]["correlation_context"]
    fake.formatter.ainvoke.assert_awaited_once()


@pytest.mark.parametrize("modes,prefixed", [
    ([], False), (["Synthetic"], True), (["simulation"], True), (["Synthetic", "simulation"], True),
    (["real"], False), (["Synthetic", "real"], False), (["Synthetic", None], False), ([None], False),
])
def test_synthetic_notice_comes_only_from_all_consulted_rows_without_mutating_model_output(monkeypatch, modes, prefixed):
    fake = runtime(monkeypatch)
    fake.agent.agent = fake.graph
    messages = fake.messages
    rows = [{"id": f"event-{index}", "mode": mode, "evidence_ids": []} for index, mode in enumerate(modes)]
    fake.graph.ainvoke.side_effect = lambda inputs, **kwargs: {"messages": [*inputs["messages"],
        messages.AIMessage(tool_calls=[{"id": "call-1", "name": "consultar_incidentes", "args": {"version": "v1"}}]),
        messages.ToolMessage(tool_call_id="call-1", name="consultar_incidentes", content=json.dumps(rows))]}
    fake.formatter.ainvoke.return_value = {"incident_refs": [],
        "answer": "Respuesta consultada.", "version": "v1", "actions": []}
    original_reply = dict(fake.formatter.ainvoke.return_value)
    result = asyncio.run(fake.agent.invoke(json.dumps({"question": "Eventos", "context": {"version": "v1"}})))
    reply = json.loads(result["messages"][0].content)
    notice = "Datos sintéticos de prueba; no confirman emergencias reales.\n\n" if prefixed else ""
    assert reply == {"answer": notice + original_reply["answer"], "version": "v1", "actions": [],
        "evidence_ids": [], "sensor_evidence_ids": []}
    assert fake.formatter.ainvoke.return_value == original_reply


def test_inventory_renders_each_incident_tuple_and_its_citation_instead_of_mixed_model_prose(monkeypatch):
    fake = runtime(monkeypatch)
    rows = [
        {"id": "incident-8a61752a156c89d8", "category": "infraestructura", "locality": "Kennedy",
            "severity": "high", "review_status": "pending", "mode": "Synthetic", "evidence_ids": ["infra-post", "infra-copy"],
            "last_observed_at": "2026-10-05T03:00:00-05:00", "created_at": "2026-10-01T00:00:00Z"},
        {"id": "incident-a1ec90626b57b172", "category": "movimiento_masa", "locality": "Ciudad Bolívar",
            "severity": "high", "review_status": "validated", "mode": "Synthetic", "evidence_ids": ["landslide-post"],
            "last_observed_at": "2026-10-05T09:00:00Z"},
        {"id": "incident-24b2a1bd60a87554", "category": "inundacion", "locality": "Kennedy",
            "severity": "high", "review_status": "rejected", "mode": "Synthetic", "evidence_ids": ["flood-post"]},
    ]
    original_rows = json.dumps(rows, sort_keys=True)
    state = [fake.messages.HumanMessage(content='{"question":"Enumera los eventos","context":{"version":"v1"}}')]
    state += completed_query(fake, rows=rows)
    state += completed_query(fake, rows=[rows[0]], call_id="selected-again")
    state += completed_query(fake, "consultar_sensores", rows=[{"id": "reading-1", "mode": "Synthetic"}], call_id="sensor")
    state.append(fake.messages.AIMessage(content="Preliminary", id="terminal"))
    fake.formatter.ainvoke.return_value.update(incident_refs=["I1", "I2"],
        answer="Movimiento de masa en Kennedy; severidad alta. Confirmado. Evacuar ahora.",
        evidence_ids=["invented-post"], sensor_evidence_ids=["invented-reading"],
        actions=[{"type": "focus_incident", "incident_id": "invented-incident"}])
    original_reply = json.dumps(fake.formatter.ainvoke.return_value, sort_keys=True)

    result = asyncio.run(fake.agent.final_response({"messages": state}, fake.configuration))
    reply = json.loads(result["messages"][0].content)
    assert reply == {"version": "v1", "evidence_ids": ["infra-post", "landslide-post"], "sensor_evidence_ids": [], "actions": [],
        "answer": "Datos sintéticos de prueba; no confirman emergencias reales.\n\n"
        "Mostrando 2 de 3 resultados de incidentes consultados.\n\n"
        "- Daño de infraestructura en Kennedy; severidad alta; revisión pendiente; último reporte: 2026-10-05T08:00:00 UTC; procedencia: Synthetic.\n"
        "- Movimiento de masa en Ciudad Bolívar; severidad alta; revisión validado; último reporte: 2026-10-05T09:00:00 UTC; procedencia: Synthetic."}
    assert result["messages"][0].id == "terminal"
    assert json.dumps(fake.formatter.ainvoke.return_value, sort_keys=True) == original_reply
    assert json.dumps(rows, sort_keys=True) == original_rows
    formatted = json.loads(fake.formatter.ainvoke.call_args.args[0][1].content)["queries"]
    assert [row["incident_ref"] for row in formatted[0]["rows"]] == ["I1", "I2", "I3"]
    assert formatted[1]["rows"][0]["incident_ref"] == "I1"
    assert "incident_ref" not in formatted[2]["rows"][0]
    schema = fake.llm.with_structured_output.call_args.args[0]
    assert "response_mode" not in schema["properties"] and "response_mode" not in schema["required"]
    assert schema["properties"]["incident_refs"] == {"type": "array", "maxItems": 3,
        "items": {"type": "string", "enum": ["I1", "I2", "I3"]}}
    assert "incident_refs" in schema["required"]
    assert not {"response_mode", "incident_refs"} & set(REPLY_SCHEMA["properties"])
    fake.formatter.ainvoke.assert_awaited_once()


@pytest.mark.parametrize("selected", [["I7"], ["E1"], ["I1", "I1"], "I1", [None],
    [f"I{index}" for index in range(1, 7)]])
def test_inventory_rejects_unknown_duplicate_or_excess_incident_selection(monkeypatch, selected):
    fake = runtime(monkeypatch)
    rows = [{"id": f"incident-{index}", "evidence_ids": [f"post-{index}"]} for index in range(6)]
    state = [fake.messages.HumanMessage(content='{"question":"Enumera","context":{"version":"v1"}}')]
    state += completed_query(fake, rows=rows)
    fake.formatter.ainvoke.return_value.update(incident_refs=selected)
    with pytest.raises(RuntimeError, match="Invalid incident selection"):
        asyncio.run(fake.agent.final_response({"messages": state}, fake.configuration))
    assert fake.llm.with_structured_output.call_args.args[0]["properties"]["incident_refs"]["maxItems"] == 5


def test_general_response_requires_explicit_incident_selection(monkeypatch):
    fake = runtime(monkeypatch)
    state = [fake.messages.HumanMessage(content='{"question":"Enumera","context":{"version":"v1"}}')]
    state += completed_query(fake)
    fake.formatter.ainvoke.return_value.pop("incident_refs")
    with pytest.raises(RuntimeError, match="Invalid incident selection"):
        asyncio.run(fake.agent.final_response({"messages": state}, fake.configuration))


@pytest.mark.parametrize("change,error", [
    ({"evidence_ids": []}, "no queried evidence"),
    ({"last_observed_at": "2026-10-05T03:00:00"}, "requires a timezone"),
])
def test_inventory_requires_a_row_citation_and_unambiguous_report_timestamp(monkeypatch, change, error):
    fake = runtime(monkeypatch)
    row = {"id": "incident-1", "evidence_ids": ["post-1"], **change}
    state = [fake.messages.HumanMessage(content='{"question":"Enumera","context":{"version":"v1"}}')]
    state += completed_query(fake, rows=[row])
    fake.formatter.ainvoke.return_value.update(incident_refs=["I1"])
    with pytest.raises(RuntimeError, match=error):
        asyncio.run(fake.agent.final_response({"messages": state}, fake.configuration))


def test_formatter_empty_incident_results_keep_scope_and_allow_an_explanation(monkeypatch):
    fake = runtime(monkeypatch)
    scope = {"version": "v1", "country": "Perú", "mode": "real"}
    state = [fake.messages.HumanMessage(content=json.dumps({"question": "Enumera", "context": scope}))]
    state += completed_query(fake, args=scope)
    fake.formatter.ainvoke.return_value.update(answer="No hay resultados de incidentes en la versión y filtros consultados.")
    reply = json.loads(asyncio.run(fake.agent.final_response({"messages": state}, fake.configuration))["messages"][0].content)
    assert reply == {"answer": "No hay resultados de incidentes en la versión y filtros consultados.",
        "version": "v1", "evidence_ids": [], "sensor_evidence_ids": [], "actions": []}
    assert fake.llm.with_structured_output.call_args.args[0]["properties"]["incident_refs"] == {
        "type": "array", "maxItems": 0, "items": {"type": "string"}}
    assert json.loads(fake.formatter.ainvoke.call_args.args[0][1].content)["queries"] == [
        {"tool": "consultar_incidentes", "filters": scope, "rows": []}]


@pytest.mark.parametrize("rows", [[], [{"id": "reading-1"}]])
def test_sensor_query_cannot_select_an_unqueried_incident(monkeypatch, rows):
    fake = runtime(monkeypatch)
    state = [fake.messages.HumanMessage(content='{"question":"Enumera eventos","context":{"version":"v1"}}')]
    state += completed_query(fake, "consultar_sensores", rows=rows)
    fake.formatter.ainvoke.return_value.update(incident_refs=["I1"])
    with pytest.raises(RuntimeError, match="Invalid incident selection"):
        asyncio.run(fake.agent.final_response({"messages": state}, fake.configuration))


def test_empty_incident_selection_allows_verification_explanation_even_with_queried_incidents(monkeypatch):
    fake = runtime(monkeypatch)
    state = [fake.messages.HumanMessage(content='{"question":"¿Qué falta verificar?","context":{"version":"v1"}}')]
    state += completed_query(fake, rows=[{"id": "incident-1", "evidence_ids": ["post-1"]}])
    fake.formatter.ainvoke.return_value.update(answer="Falta contrastar el lugar y la hora del reporte.", evidence_ids=["E1"])
    reply = json.loads(asyncio.run(fake.agent.final_response({"messages": state}, fake.configuration))["messages"][0].content)
    assert reply == {"answer": "Falta contrastar el lugar y la hora del reporte.", "version": "v1",
        "evidence_ids": ["post-1"], "sensor_evidence_ids": [], "actions": []}


@pytest.mark.parametrize("selection", [{"incident_id": "incident-1"}, {"sensor_id": "sensor-1"},
    {"incident_id": "incident-1", "sensor_id": "sensor-1"}])
def test_selected_context_keeps_source_comparison_and_sensor_analysis_without_inventory_schema(monkeypatch, selection):
    fake = runtime(monkeypatch)
    request = {"question": "Compara las dos redes y la lectura disponible antes de reportarlo.",
        "context": {"version": "v1", **selection}}
    state = [fake.messages.HumanMessage(content=json.dumps(request))]
    state += completed_query(fake, rows=[{"id": "incident-1", "evidence_ids": ["post-1", "post-2"],
        "category": "inundacion", "locality": "Bosa", "severity": "high", "review_status": "pending"}])
    state += completed_query(fake, "consultar_evidencia", rows=[{"id": "post-1", "platform": "facebook"},
        {"id": "post-2", "platform": "x"}], call_id="evidence")
    state += completed_query(fake, "consultar_sensores", rows=[{"id": "reading-1", "sensor_id": "sensor-1",
        "value": 3.92, "unit": "m", "status": "warning"}], call_id="sensor")
    answer = "Facebook y X reportan inundación en Bosa, pendiente de revisión. El sensor marca 3.92 m, warning; no confirma el reporte."
    fake.formatter.ainvoke.return_value = {"answer": answer, "version": "v1", "evidence_ids": ["E1", "E2"],
        "sensor_evidence_ids": ["S1"], "actions": []}
    reply = json.loads(asyncio.run(fake.agent.final_response({"messages": state}, fake.configuration))["messages"][0].content)
    assert reply == {"answer": reply["answer"], "version": "v1", "evidence_ids": ["post-1", "post-2"],
        "sensor_evidence_ids": ["reading-1"], "actions": []}
    assert "3.92 m; estado warning" in reply["answer"] and "revisión del incidente: pending" in reply["answer"]
    assert answer not in reply["answer"] and "Texto no disponible" in reply["answer"]
    schema = fake.llm.with_structured_output.call_args.args[0]
    assert "incident_refs" not in schema["properties"] and "incident_refs" not in schema["required"]
    assert schema["additionalProperties"] is False
    messages = fake.formatter.ainvoke.call_args.args[0]
    assert "selecciona hasta cinco incident_refs" not in messages[0].content
    source = json.loads(messages[1].content)
    assert source["request"] == request
    assert all("incident_ref" not in row for query in source["queries"] for row in query["rows"])
    fake.formatter.ainvoke.assert_awaited_once()


@pytest.mark.parametrize("selection", [{"incident_id": "incident-1"}, {"sensor_id": "sensor-1"},
    {"incident_id": "incident-1", "sensor_id": "sensor-1"}])
def test_selected_context_rejects_unexpected_inventory_references(monkeypatch, selection):
    fake = runtime(monkeypatch)
    state = [fake.messages.HumanMessage(content=json.dumps({"question": "¿Qué falta verificar?",
        "context": {"version": "v1", **selection}}))]
    state += completed_query(fake, rows=[{"id": "incident-1", "evidence_ids": ["post-1"]}])
    fake.formatter.ainvoke.return_value.update(incident_refs=["I1"])
    with pytest.raises(RuntimeError, match="Invalid incident selection"):
        asyncio.run(fake.agent.final_response({"messages": state}, fake.configuration))


def test_inventory_missing_report_date_does_not_use_record_creation_date(monkeypatch):
    fake = runtime(monkeypatch)
    state = [fake.messages.HumanMessage(content='{"question":"Enumera","context":{"version":"v1"}}')]
    state += completed_query(fake, rows=[{"id": "incident-1", "evidence_ids": ["post-1"], "created_at": "2026-10-01T00:00:00Z"}])
    fake.formatter.ainvoke.return_value.update(incident_refs=["I1"])
    reply = json.loads(asyncio.run(fake.agent.final_response({"messages": state}, fake.configuration))["messages"][0].content)
    assert "último reporte: no disponible" in reply["answer"] and "2026-10-01" not in reply["answer"]


@pytest.mark.parametrize("question,answer", [
    ("Hola", "Hola, ¿en qué puedo ayudarte?"),
    ("¿Qué falta verificar?", "Falta contrastar lugar y hora; esta lectura no confirma un incidente."),
    ("¿Y el sensor ahora?", "Nivel de río: 3.92 m, warning, 2026-10-05T09:00:00 UTC."),
])
def test_explanation_preserves_greeting_certainty_and_sensor_followup_text(monkeypatch, question, answer):
    fake = runtime(monkeypatch)
    state = [fake.messages.HumanMessage(content=json.dumps({"question": question, "context": {"version": "v1"}}))]
    state += completed_query(fake, "consultar_sensores", rows=[{"id": "reading-1", "value": 3.92, "unit": "m",
        "status": "warning", "observed_at": "2026-10-05T09:00:00Z"}])
    fake.formatter.ainvoke.return_value.update(answer=answer, sensor_evidence_ids=["S1"])
    reply = json.loads(asyncio.run(fake.agent.final_response({"messages": state}, fake.configuration))["messages"][0].content)
    assert reply == {"answer": answer, "version": "v1", "evidence_ids": [], "sensor_evidence_ids": ["reading-1"], "actions": []}


@pytest.mark.parametrize("failure", ["invented_evidence", "invented_sensor", "history_evidence", "wrong_version", "formatter_error",
    "raw_id_instead_of_token", "wrong_token_collection", "non_list_citations"])
def test_formatter_failures_or_citations_outside_current_queries_do_not_return_an_answer(monkeypatch, failure):
    fake = runtime(monkeypatch)
    fake.agent.agent = fake.graph
    messages = fake.messages

    def response(inputs, *, config):
        return {"messages": [messages.HumanMessage(id="earlier", content="Earlier query"),
            messages.AIMessage(tool_calls=[{"id": "earlier-call", "name": "consultar_evidencia", "args": {"version": "v1"}}]),
            messages.ToolMessage(tool_call_id="earlier-call", name="consultar_evidencia", content='[{"id":"history-post"}]'),
            *inputs["messages"],
            messages.AIMessage(tool_calls=[{"id": "current-call", "name": "consultar_incidentes", "args": {"version": "v1"}}]),
            messages.ToolMessage(tool_call_id="current-call", name="consultar_incidentes",
                content='[{"id":"incident-1","evidence_ids":["post-1"]}]'),
            messages.AIMessage(content="Preliminary answer must not leak on formatter failure")]}

    fake.graph.ainvoke.side_effect = response
    reply = fake.formatter.ainvoke.return_value
    if failure == "formatter_error":
        fake.formatter.ainvoke.side_effect = RuntimeError("Formatting failed")
    elif failure == "wrong_version":
        reply["version"] = "v0"
    elif failure == "invented_sensor":
        reply["sensor_evidence_ids"] = ["invented-reading"]
    elif failure == "raw_id_instead_of_token":
        reply["evidence_ids"] = ["post-1"]
    elif failure == "wrong_token_collection":
        reply["sensor_evidence_ids"] = ["E1"]
    elif failure == "non_list_citations":
        reply["evidence_ids"] = "E1"
    else:
        reply["evidence_ids"] = ["history-post" if failure == "history_evidence" else "invented-post"]
    with pytest.raises(RuntimeError, match="Formatting failed|outside the current queries"):
        asyncio.run(fake.agent.invoke(json.dumps({"question": "Eventos", "context": {"version": "v1"}})))
    fake.formatter.ainvoke.assert_awaited_once()
    fake.llm.with_structured_output.assert_called_once()
    fake.graph.aupdate_state.assert_not_awaited()


def test_each_agent_owns_its_inference_client_without_cached_remote_state(monkeypatch):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    TerritorialAgent().setup()
    clients = [item.kwargs["client"] for item in fake.init_llm.call_args_list]
    signers = [item.kwargs["signer"] for item in fake.inference_client.call_args_list]
    assert len(clients) == len(signers) == 2
    assert clients[0] is not clients[1] and signers[0] is not signers[1]


@pytest.mark.parametrize("reader", [None, "TerritorialReaderRuntime"])
def test_explicit_model_and_shared_credential_replace_stale_reader_model_only(monkeypatch, reader):
    fake = runtime(monkeypatch)
    config = {"region": "us-chicago-1", "compartment_id": "test-compartment", "model_id": "governance-model",
        "oci_credential_name": "AidpDataGovernanceExtension", "oci_identity_sha256": identity_hash(fake.credentials),
        "catalog": "oci_medallion", "gold_query_compute_id": "gold-query-compute"}
    if reader is not None:
        config["reader_credential_name"] = reader
    monkeypatch.setattr("app.territorial.agent.RUNTIME_CONFIG", config)
    fake.agent.setup()
    assert fake.init_llm.call_args.kwargs["model_id"] == "governance-model"
    assert fake.secret_get.call_args_list == [call(name="AidpDataGovernanceExtension", key=key)
        for key in ("tenancy", "user", "fingerprint", "private_key")]
    tools = {tool.__name__: tool for tool in fake.create_agent.call_args.args[1]}
    fake.cursor.fetchall.return_value = []
    tools["consultar_sensores"]("publication")
    fake.database.assert_not_called()
    assert fake.gold_query.call_args.args[0] is config
    fake.gold_query.reset_mock()
    fake.gold_query.side_effect = PermissionError("Gold compute unavailable")
    with pytest.raises(PermissionError):
        tools["consultar_sensores"]("publication")
    assert fake.gold_query.call_count == 1
    fake.database.assert_not_called()


def test_agent_missing_deployment_config_never_discovers_a_legacy_reader(monkeypatch):
    fake = runtime(monkeypatch)
    monkeypatch.setattr("app.territorial.agent.RUNTIME_CONFIG", None)
    with pytest.raises(RuntimeError, match="deployment configuration incomplete"):
        fake.agent.setup()
    fake.secret_get.assert_not_called()
    fake.gold_query.assert_not_called()


@pytest.mark.parametrize("factory", [signer, runtime_auth])
@pytest.mark.parametrize("credential", ["AidpDataGovernanceExtension", "TerritorialWriterRuntime", "PrismaWriterRuntime"])
def test_shared_identity_drift_fails_before_private_key_read_without_fallback(monkeypatch, factory, credential):
    fake = runtime(monkeypatch)
    expected = identity_hash(fake.credentials)
    fake.credentials["user"] = "another-user"
    args = (fake.secret_get,) + (("us-chicago-1",) if factory is runtime_auth else ())
    with pytest.raises(RuntimeError, match="identity mismatch"):
        factory(*args, credential_name=credential, expected_identity=expected)
    assert fake.secret_get.call_args_list == [call(name=credential, key=key)
        for key in ("tenancy", "user", "fingerprint")]
    fake.native_signer.assert_not_called()


def test_shared_credential_selector_prefers_governance_and_rejects_invalid_preferred():
    writer = {"displayName": "PrismaWriterRuntime", "credentialType": "SECRET_TOKEN", "lifeCycleState": "ACTIVE", "key": "writer"}
    canonical = {**writer, "displayName": "TerritorialWriterRuntime", "key": "canonical"}
    governance = {"displayName": "AidpDataGovernanceExtension", "type": "SECRET_TOKEN", "lifecycleState": "ACTIVE", "key": "governance"}
    assert shared_credential([writer, governance, canonical]) is governance
    assert shared_credential([writer, canonical]) is canonical
    assert shared_credential([writer, {**canonical, "lifeCycleState": "DELETED"}]) is writer
    assert shared_credential([writer]) is writer
    assert shared_credential([]) is None
    for invalid in ([writer, governance, dict(governance)], [writer, {**governance, "type": "VAULT_REFERENCE"}],
                    [writer, {**governance, "lifecycleState": "FAILED"}],
                    [writer, canonical, dict(canonical)], [writer, {**canonical, "credentialType": "VAULT_REFERENCE"}],
                    [writer, {**canonical, "lifeCycleState": "FAILED"}], [writer, {**canonical, "key": ""}]):
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
    incident = {"id": "incident-42", "locality": "Kennedy", "category": "inundacion", "severity": "high",
        "lat": 4.6, "lon": -74.15, "evidence_ids": ["post-42"], "review_status": "pending", "mode": "Synthetic"}
    evidence = {"id": "post-42", "platform": "x", "text": "Reporte de inundación", "locality": "Kennedy",
        "created_at": "2026-10-04T14:20:00Z", "mode": "Synthetic"}
    fake.cursor.fetchall.side_effect = [[(io.StringIO(json.dumps(sensor)),)], [(json.dumps(incident),)], [(json.dumps(evidence),)], []]
    common = {"version": "publication-42", "locality": "Kennedy", "bbox": "-74.2,4.5,-74.1,4.7",
        "date_from": "2026-10-04T09:00:00-05:00", "date_to": "2026-10-04T14:30:00Z"}
    assert tools["consultar_sensores"](**common, sensor_id=sensor["sensor_id"], sensor_type="rainfall") == [sensor]
    assert tools["consultar_incidentes"](**common, incident_id="incident-42", category="inundacion", severity="high",
        mode="simulation", platform="x", country="Colombia", city="Bogotá") == [{**incident, "evidence_count": 1,
            "reviewed_evidence_ids": [], "reviewed_evidence_count": 0}]
    assert tools["consultar_evidencia"]("publication-42", "post-42") == [{**evidence, "incident_relations": []}]

    sensor_sql, sensor_binds = fake.cursor.execute.call_args_list[0].args
    incident_sql, incident_binds = fake.cursor.execute.call_args_list[1].args
    evidence_sql, evidence_binds = fake.cursor.execute.call_args_list[2].args
    relation_sql, relation_binds = fake.cursor.execute.call_args_list[3].args
    scope = {"version": "publication-42", "locality": "Kennedy", "west": -74.2, "south": 4.5, "east": -74.1, "north": 4.7,
        "date_from": "2026-10-04T14:00:00+00:00", "date_to": "2026-10-04T14:30:00+00:00"}
    assert sensor_binds == {**scope, "sensor_id": sensor["sensor_id"], "sensor_type": "rainfall"}
    assert incident_binds == {**scope, "incident_id": "incident-42", "category": "inundacion", "severity": "high",
        "source_mode": "Synthetic", "platform": "x", "country": "Colombia", "city": "Bogotá"}
    assert evidence_binds == {"version": "publication-42", "evidence_id": "post-42", "incident_id": None, "platform": None}
    assert "territorial_sensors WHERE publication_version=:version" in sensor_sql
    assert "territorial_incidents i WHERE i.publication_version=:version" in incident_sql
    assert "e.publication_version=i.publication_version" in incident_sql
    assert "territorial_evidence e WHERE e.publication_version=:version" in evidence_sql
    assert "LIMIT 10" in evidence_sql
    assert "territorial_event_posts r" in relation_sql and "WHERE r.publication_version=:version" in relation_sql
    assert "r.post_key IN (:post_0)" in relation_sql and "r.event_id=:incident_id" in relation_sql
    assert relation_binds == {"version": "publication-42", "incident_id": None, "post_0": "post-42"}
    for sql in (sensor_sql, incident_sql):
        assert "LIMIT 100" in sql and "publication-42" not in sql and "Kennedy" not in sql
    assert fake.gold_query.call_count == 4
    fake.database.assert_not_called()


def test_evidence_tool_executes_linked_versioned_platform_query_and_recovers_unsampled_sources(monkeypatch, incident_database):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    tools = {tool.__name__: tool for tool in fake.create_agent.call_args.args[1]}
    def execute(sql, binds):
        fake.cursor.fetchall.return_value = sqlite_rows(incident_database, sql, binds)
    fake.cursor.execute.side_effect = execute
    evidence = tools["consultar_evidencia"]
    assert [row["id"] for row in evidence("v1", "co-high-0")] == ["co-high-0"]
    assert [row["id"] for row in evidence("v1", incident_id="different-publications")] == ["different-publications-1", "different-publications-0"]
    assert [row["id"] for row in evidence("v1", incident_id="different-publications", platform="facebook")] == ["different-publications-0"]
    assert evidence("v1", "co-high-0", incident_id="co-low") == []
    assert evidence("v1", "co-high-0", platform="facebook") == []
    assert evidence("v2", "co-high-0") == []
    assert evidence("v2", incident_id="co-high") == []
    assert evidence("v1", incident_id="missing") == []
    assert evidence("v1", "unknown-country-0")[0]["country"] is None
    assert evidence("v2", "unknown-country-0")[0]["country"] == "Colombia"
    for filters in ({"evidence_id": "post' OR 1=1 --"}, {"incident_id": "event' OR 1=1 --"},
                    {"incident_id": "different-publications", "platform": "facebook' OR 1=1 --"}):
        assert evidence("v1", **filters) == []
        assert all(value not in fake.cursor.execute.call_args.args[0] for value in filters.values())

    refs = [f"sample-{index:02}" for index in range(14)]
    incident_database.execute("INSERT INTO ADMIN.PRISMA_V_INCIDENTS VALUES (?,?,?,?,?,?,?)",
        ("v1", "sampled", "Kennedy", "inundacion", "high", "Synthetic", json.dumps({"id": "sampled", "evidence_ids": refs})))
    for index, ref in enumerate(refs):
        platform = "x" if index < 12 else "facebook"
        row = {"id": ref, "platform": platform, "created_at": f"2026-10-05T14:{index:02}:00Z"}
        incident_database.execute("INSERT INTO ADMIN.PRISMA_V_EVIDENCE VALUES (?,?,?,?)", ("v1", ref, platform, json.dumps(row)))
    sampled = tools["consultar_incidentes"]("v1", incident_id="sampled")[0]
    assert sampled["evidence_ids"] == refs[:5] and sampled["evidence_count"] == 14
    assert [row["id"] for row in evidence("v1", incident_id="sampled")] == [refs[13], refs[11], refs[12], *refs[10:3:-1]]
    assert [row["id"] for row in evidence("v1", incident_id="sampled", platform="facebook")] == refs[-2:][::-1]
    assert [row["id"] for row in evidence("v1", refs[-1], incident_id="sampled", platform="facebook")] == [refs[-1]]
    # One busy network cannot hide the second network behind the ten-row limit.
    incident_database.execute("UPDATE ADMIN.PRISMA_V_EVIDENCE SET evidence_json=json_set(evidence_json,'$.created_at','2026-10-04T14:00:00Z') WHERE platform='facebook'")
    rows = evidence("v1", incident_id="sampled")
    assert {row["platform"] for row in rows[:2]} == {"x", "facebook"}
    assert len(rows) == 10


def test_evidence_tool_exposes_copy_and_claim_relations_from_exact_snapshot_and_event(monkeypatch, incident_database):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    evidence = next(tool for tool in fake.create_agent.call_args.args[1] if tool.__name__ == "consultar_evidencia")
    relations = [
        {"event_id": "different-publications", "post_key": "different-publications-0", "relation": "duplicate",
            "claim_relation": "supports", "duplicate_of": "source-root", "explanation": "Exact image SHA-256 matches an earlier report", "analysis_version": "v14-test"},
        {"event_id": "different-publications", "post_key": "different-publications-1", "relation": "duplicate",
            "claim_relation": "contradicts", "duplicate_of": "source-root", "explanation": "Exact normalized content matches an earlier report", "analysis_version": "v14-test"},
        {"event_id": "other-event", "post_key": "different-publications-0", "relation": "unclassified", "duplicate_of": None},
        {"event_id": "different-publications", "post_key": "unrequested-post", "relation": "supports", "duplicate_of": None},
    ]
    relations.append({**relations[0], "claim_relation": "contradicts", "explanation": "A separate caption disputes the claim"})
    other_version = [{**relations[0], "relation": "unclassified", "duplicate_of": None}]
    for version, rows in (("v1", relations), ("v2", other_version)):
        incident_database.execute("INSERT INTO ADMIN.PRISMA_V_SNAPSHOTS VALUES (?,?)",
            (version, json.dumps({"version": version, "event_posts": rows})))
    incident_database.execute("INSERT INTO ADMIN.PRISMA_V_EVIDENCE SELECT 'v2',evidence_id,platform,evidence_json FROM ADMIN.PRISMA_V_EVIDENCE WHERE version='v1' AND evidence_id='different-publications-0'")
    def execute(sql, binds):
        fake.cursor.fetchall.return_value = sqlite_rows(incident_database, sql, binds)
    fake.cursor.execute.side_effect = execute

    result = evidence("v1", incident_id="different-publications")
    assert {row["id"]: {json.dumps(item, sort_keys=True) for item in row["incident_relations"]} for row in result} == {
        "different-publications-0": {json.dumps(relations[index], sort_keys=True) for index in (0, 4)},
        "different-publications-1": {json.dumps(relations[1], sort_keys=True)}}
    assert len(result) == 2  # Adding relations must not duplicate evidence rows or count copies as new posts.
    exact = evidence("v1", evidence_id="different-publications-0", incident_id="different-publications", platform="facebook")
    assert len(exact) == 1 and len(exact[0]["incident_relations"]) == 2
    assert {json.dumps(item, sort_keys=True) for item in exact[0]["incident_relations"]} == {
        json.dumps(relations[index], sort_keys=True) for index in (0, 4)}
    isolated = evidence("v1", evidence_id="different-publications-0")
    assert len(isolated) == 1 and len(isolated[0]["incident_relations"]) == 3
    assert {json.dumps(item, sort_keys=True) for item in isolated[0]["incident_relations"]} == {
        json.dumps(relations[index], sort_keys=True) for index in (0, 2, 4)}
    assert evidence("v2", evidence_id="different-publications-0")[0]["incident_relations"] == other_version
    assert evidence("v1", evidence_id="co-high-0")[0]["incident_relations"] == []
    original = json.loads(incident_database.execute("SELECT evidence_json FROM ADMIN.PRISMA_V_EVIDENCE WHERE version='v1' AND evidence_id='different-publications-0'").fetchone()[0])
    assert "incident_relations" not in original
    for item in fake.cursor.execute.call_args_list:
        sql, binds = item.args
        assert binds["version"] in {"v1", "v2"} and binds["version"] not in sql
        if "territorial_event_posts" in sql:
            assert "WHERE r.publication_version=:version" in sql and "r.event_id=:incident_id" in sql
            assert all(value not in sql for value in binds.values() if isinstance(value, str))
    fake.database.assert_not_called()


def test_evidence_relation_database_error_does_not_become_an_empty_relation_list(monkeypatch):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    evidence = next(tool for tool in fake.create_agent.call_args.args[1] if tool.__name__ == "consultar_evidencia")
    fake.cursor.fetchall.side_effect = [[(json.dumps({"id": "post-1"}),)], RuntimeError("Snapshot unavailable")]
    with pytest.raises(RuntimeError, match="Snapshot unavailable"):
        evidence("v1", "post-1")
    assert fake.gold_query.call_count == 2
    fake.database.assert_not_called()


@pytest.mark.parametrize("filters", [
    {}, {"platform": "x"}, {"version": "", "evidence_id": "post"}, {"version": None, "evidence_id": "post"},
    {"version": "v" * 101, "evidence_id": "post"}, {"evidence_id": "p" * 201}, {"incident_id": "i" * 201},
    {"incident_id": "incident", "platform": "p" * 201}, {"evidence_id": None}, {"incident_id": 42},
    {"evidence_id": " "}, {"incident_id": " event"}, {"incident_id": "event", "platform": " x "},
])
def test_evidence_tool_rejects_missing_ids_and_invalid_filters_before_database(monkeypatch, filters):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    tool = next(item for item in fake.create_agent.call_args.args[1] if item.__name__ == "consultar_evidencia")
    with pytest.raises(ValueError):
        tool(**{"version": "v1", **filters})
    fake.database.assert_not_called()


@pytest.mark.parametrize("count,reviewed_count", [(0, 0), (3, 1), (5, 8), (200, 200)])
def test_incident_tool_samples_citations_without_mutating_rows_or_losing_context(monkeypatch, count, reviewed_count):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    tool = next(item for item in fake.create_agent.call_args.args[1] if item.__name__ == "consultar_incidentes")
    incident = {"id": "incident-42", "evidence_ids": [f"post-{index}" for index in range(count)],
        "mode": "Synthetic", "is_simulated": True, "review_status": "pending", "review_note": "Awaiting review",
        "reviewed_evidence_ids": [f"reviewed-{index}" for index in range(reviewed_count)], "corroboration_score": 75, "confidence": 0.9,
        "created_at": "2026-10-04T14:00:00Z", "updated_at": "2026-10-04T15:00:00Z",
        "last_observed_at": "2026-10-04T14:55:00Z", "report_counts": {"x": {"30": count}},
        "report_activity_by_platform": {"x": "high"}, "severity": "high", "locality": "Kennedy"}
    original = json.dumps(incident)
    fake.cursor.fetchall.return_value = [(original,)]
    # Reuse the decoded row so an in-place slice/assignment would fail this regression.
    with patch("app.territorial.agent.json.loads", return_value=incident):
        result = tool("v1")
    assert incident == json.loads(original)
    assert result == [{**incident, "evidence_ids": incident["evidence_ids"][:5], "evidence_count": count,
        "reviewed_evidence_ids": incident["reviewed_evidence_ids"][:5], "reviewed_evidence_count": reviewed_count}]
    assert result[0] is not incident and result[0]["evidence_ids"] is not incident["evidence_ids"]
    assert result[0]["reviewed_evidence_ids"] is not incident["reviewed_evidence_ids"]


def test_incident_tool_exposes_real_enums_and_separate_open_geographic_fields(monkeypatch):
    fake = runtime(monkeypatch)
    fake.agent.setup()
    tool = next(item for item in fake.create_agent.call_args.args[1] if item.__name__ == "consultar_incidentes")
    annotations = get_type_hints(tool)
    arguments = create_model("IncidentArguments", **{name: (annotations[name],
        ... if parameter.default is inspect.Parameter.empty else parameter.default)
        for name, parameter in inspect.signature(tool).parameters.items()})
    properties = arguments.model_json_schema()["properties"]
    for field, values in (("category", CATEGORY_NAMES), ("severity", SEVERITIES)):
        assert properties[field]["anyOf"] == [{"enum": list(values), "type": "string"}, {"type": "null"}]
        assert properties[field]["default"] is None
        assert field not in arguments.model_json_schema()["required"]
        with pytest.raises(ValidationError):
            arguments(version="v1", **{field: ""})
    assert all(properties[key]["type"] == "string" and "enum" not in properties[key] for key in ("locality", "country", "city"))
    assert arguments(version="v1", country="Colombia", city="Cali", locality="Comuna 20", severity="high").severity == "high"
    for invalid in ({"category": "flood"}, {"severity": "critical"}):
        with pytest.raises(ValidationError):
            arguments(version="v1", **invalid)
        with pytest.raises(ValueError):
            tool("v1", **invalid)
    fake.database.assert_not_called()
    fake.cursor.fetchall.return_value = []
    for optional in ({}, {"category": None, "severity": None}):
        parsed = arguments(version="v1", country="Colombia", **optional)
        assert parsed.category is None and parsed.severity is None
        assert tool(**parsed.model_dump()) == []
        binds = fake.cursor.execute.call_args.args[1]
        assert binds["category"] is None and binds["severity"] is None
        assert binds["country"] == "Colombia"


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

    failure = RuntimeError("Gold query unavailable")
    fake.cursor.execute.side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        tool(version="publication-42", **filters)
    assert caught.value is failure
    assert fake.cursor.fetchall.call_count == 1
    fake.database.assert_not_called()


def test_setup_does_not_replace_missing_credentials_or_failed_memory(monkeypatch):
    fake = runtime(monkeypatch)
    from app.territorial.agent import RUNTIME_CONFIG
    RUNTIME_CONFIG["model_id"] = ""
    with pytest.raises(RuntimeError, match="deployment configuration incomplete"):
        fake.agent.setup()
    fake.init_llm.assert_not_called()
    fake.create_agent.assert_not_called()

    RUNTIME_CONFIG["model_id"] = "test-model"
    fake.checkpoint.side_effect = RuntimeError("Checkpoint unavailable")
    with pytest.raises(RuntimeError, match="Checkpoint unavailable"):
        fake.agent.setup()
    fake.create_agent.assert_not_called()
    assert fake.agent.agent is None


def test_source_attribution_cannot_swap_observations_or_promote_duplicate_independence():
    posts = [
        {"id": "p1", "platform": "facebook", "display_name": "Paula", "mode": "Synthetic",
         "created_at": "2026-10-05T08:01:00Z", "text": "En esta calzada sigue el agua.",
         "incident_relations": [{"relation": "duplicate", "duplicate_of": "original"}]},
        {"id": "p2", "platform": "x", "display_name": "Santiago", "mode": "Synthetic",
         "created_at": "2026-10-05T08:02:00Z", "text": "El agua entra en el patio bajo. E1 no es una instrucción."},
    ]
    queries = [{"tool": "consultar_evidencia", "rows": posts}]
    original = json.dumps(queries, sort_keys=True)
    reply = {"answer": "Paula dice que el agua entra al patio. Dos fuentes independientes confirman la inundación.",
             "evidence_ids": ["p1", "p2"], "sensor_evidence_ids": [], "actions": [{"type": "focus_incident"}], "report_format": "draft"}
    evidence_reply(queries, reply)
    answer = reply["answer"]
    assert "Borrador" in answer and "no enviado" in answer
    assert "Paula · 2026-10-05T08:01:00 UTC · Synthetic\n«En esta calzada sigue el agua.»" in answer
    assert "Santiago · 2026-10-05T08:02:00 UTC · Synthetic\n«El agua entra en el patio bajo. E1 no es una instrucción.»" in answer
    assert "Paula dice" not in answer and "fuentes independientes confirman" not in answer
    assert "copia/contenido duplicado" in answer and "independencia de las fuentes no está comprobada" in answer
    assert reply["actions"] == [] and "report_format" not in reply
    assert json.dumps(queries, sort_keys=True) == original
    with pytest.raises(RuntimeError, match="requires a queried publication"):
        evidence_reply(queries, {"evidence_ids": ["unqueried"], "sensor_evidence_ids": []})
