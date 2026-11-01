"""AIDP setup/invoke binding; native identity and memory remain authoritative."""
from contextlib import aclosing
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from . import APIFY_CREDENTIAL_NAME
from .model import DecisionModel
from .provider import ApifyProvider
from .repository import Scope, ObjectSnapshot, Journal, digest, projection_statements, projection_checks
from .workflow import MarketWorkflow
from .execution import ProviderPending
from .streaming import run_turn, stream_turn


def verify_projection(result, expected):
    if isinstance(result, str):
        result = json.loads(result)
    for _ in range(3):
        if not isinstance(result, dict) or result.get('error') or result.get('isError') or (result.get('metadata') or {}).get('error'):
            raise RuntimeError('Delta projection readback failed')
        if 'rows' in result:
            break
        result = result.get('result', result.get('structuredContent'))
    rows = result.get('rows') if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError('Delta projection has no verified readback')
    counts = {key.lower(): value for key, value in rows[0].items()}
    if any(type(counts.get(key)) is not int or counts[key] != expected for key in ('total_rows', 'distinct_keys', 'matched_rows')):
        raise RuntimeError('Delta projection does not match the exact scoped records')


async def chat_request(user_query, trace_id, graph, config):
    """Keep explicit command IDs; bind ordinary chat to its native invocation."""
    if not isinstance(user_query, str) or not user_query.strip():
        raise ValueError('Question must contain 1–500 characters')
    question = user_query.strip()
    if question.startswith('{'):
        request = json.loads(question)
        if not isinstance(request, dict):
            raise ValueError('The chat command must be an object')
        return request
    if len(question) > 500:
        raise ValueError('Question must contain 1–500 characters')
    session = config.get('configurable', {}).get('thread_id')
    if not isinstance(session, str) or not session.strip() or not isinstance(trace_id, str) or not 1 <= len(trace_id.strip()) <= 128:
        raise ValueError('An authenticated native session and invocation ID are required')
    previous = await graph.aget_state(config)
    if previous.next:
        values = previous.values or {}
        if values.get('question') != question or not values.get('request_id'):
            raise RuntimeError('Resume the interrupted request before starting a new turn')
        return {'request_id': values['request_id'], 'question': question}
    return {'request_id': 'native:' + digest([session, trace_id]), 'question': question}


class AgentRuntime:
    def setup(self):
        import aidputils
        import oci
        from aidputils.agents.toolkit.agent_helper import GenAIChatInvoker, GenerativeAiInferenceV2Client
        from aidputils.agents.toolkit.tool_helper import build_structured_tools_from_allowed_mcp_tools
        from aidputils.agents.tools.mcp.mcp_service import get_mcp_client
        self.config = json.loads(Path(__file__).with_name('runtime.json').read_text(encoding='utf-8'))
        config = self.config
        identity = {key: aidputils.secrets.get(name=config['credential_name'], key=key)
                    for key in ('tenancy', 'user', 'fingerprint', 'private_key')}
        checksum = hashlib.sha256(json.dumps([identity[key] for key in ('tenancy', 'user', 'fingerprint')], separators=(',', ':')).encode()).hexdigest()
        if checksum != config['identity_sha256']:
            raise ValueError('OCI runtime identity differs from the installation binding')
        signer = oci.signer.Signer(identity['tenancy'], identity['user'], identity['fingerprint'],
                                  private_key_file_location=None, private_key_content=identity['private_key'])
        storage_config = {key: identity[key] for key in ('tenancy', 'user', 'fingerprint')}
        storage_config.update(key_content=identity['private_key'], region=config['region'])
        self.storage = oci.object_storage.ObjectStorageClient(storage_config, signer=signer)
        contract = json.loads(Path(__file__).with_name('mcp_tools.json').read_text(encoding='utf-8'))
        token = aidputils.secrets.get(name=APIFY_CREDENTIAL_NAME, key='token')
        tools = build_structured_tools_from_allowed_mcp_tools(
            allowed_tools=[{'tool': tool, 'instruction': '', 'argOverrides': {}} for tool in contract['tools']],
            server_name='AiMarketIntelligenceApify', endpoint=contract['endpoint'], transport='streamable_http',
            auth={'authType': 'BEARER_TOKEN', 'token': token}, headers={})
        self.mcp_client = get_mcp_client(server_name='AiMarketIntelligenceApify', server_url=contract['endpoint'],
            auth={'authType': 'BEARER_TOKEN', 'token': token}, transport='streamable_http')
        self.provider = ApifyProvider({tool.name: tool for tool in tools})
        self.model = DecisionModel(GenAIChatInvoker(provider='generic', auth_type='API_KEY',
            client=GenerativeAiInferenceV2Client(endpoint=config['model_endpoint'], signer=signer),
            is_stream=False, compartment_id=config['compartment_id'], service_endpoint=config['model_endpoint'],
            model_id=config['model_id'], model_kwargs={'temperature': 0, 'max_tokens': 3000},
            guardrails_config=config['guardrails_config']))

    async def verify_mcp(self):
        tools = await self.mcp_client.get_tools(server_name='AiMarketIntelligenceApify', use_cached=False)
        if not {'call-actor', 'get-actor-run', 'get-dataset-items'}.issubset({tool.name for tool in tools}):
            raise RuntimeError('MCP did not expose the reviewed research tools')
        return True

    async def project(self, journal):
        from aidputils.agents.toolkit.configs import AIDPToolConf
        from aidputils.agents.toolkit.tool_helper import create_langgraph_tool
        config = self.config
        lease = str(uuid4())
        def claim(records, state):
            if state.get('projection_lease'):
                raise RuntimeError('Another projection owns this scope; retry delivery, not capture')
            state['projection_lease'] = lease
            return records
        from langgraph.config import get_stream_writer
        try:
            progress = get_stream_writer()
        except RuntimeError:
            progress = lambda event: None
        async def execute(statement):
            tool = create_langgraph_tool(AIDPToolConf(name='ai_mi_projection', description='Project validated owner-scoped research records',
                tool_class='SQLTool', conf={'catalogType': 'STANDARD', 'clusterKey': config['gold_query_compute_id'],
                'catalogKey': config['catalog'], 'schemaKey': config['framework_schema'], 'query': statement,
                'isRowLimitEnabled': True, 'maxRows': 1}, params=[]).model_dump())
            result = await tool.ainvoke({})
            if isinstance(result, str):
                result = json.loads(result)
            if not isinstance(result, dict) or result.get('error') or result.get('isError') or (result.get('metadata') or {}).get('error'):
                raise RuntimeError('Delta projection was not accepted; retry projection without recapture')
            if set(result) == {'tool_name', 'arguments'}:
                raise RuntimeError('SQL helper returned a stub; no projection verified')
            return result
        records = journal.transact(claim)
        try:
            writes = list(projection_statements(records, config['catalog'], config['framework_schema']))
            checks = list(projection_checks(records, config['catalog'], config['framework_schema']))
            total = len(writes) + len(checks)
            def notify(completed):
                progress({'stage': 'project_records', 'completed_steps': completed,
                          'total_steps': total})
            notify(0)
            for completed, statement in enumerate(writes, 1):
                await execute(statement)
                notify(completed)
            for completed, (statement, expected) in enumerate(checks, len(writes) + 1):
                verify_projection(await execute(statement), expected)
                notify(completed)
        finally:
            def release(current, state):
                if state.get('projection_lease') != lease:
                    raise RuntimeError('Projection ownership changed')
                state['projection_lease'] = None
                return current.tables == records.tables
            current = journal.transact(release)
        if not current:
            raise RuntimeError('Records changed during projection; retry delivery without recapture')

    async def invoke(self, user_query, **kwargs):
        from aidputils.agents.toolkit.agent_helper import pre_invoke_setup
        native_config = pre_invoke_setup(**kwargs)
        messages = self.stream_messages(user_query, native_config, kwargs)
        if kwargs.get('stream', False):
            return messages
        async with aclosing(messages):
            async for message in messages:
                if message.additional_kwargs['ai_mi_event'] == 'result':
                    return {'messages': [message]}

    async def stream_messages(self, user_query, native_config, kwargs):
        from aidputils.agents.toolkit.agent_helper import pre_tool_setup, post_tool_setup
        from langchain_core.messages import AIMessage
        token = pre_tool_setup(**kwargs)
        try:
            yield AIMessage(content='Revisando la solicitud.', additional_kwargs={'ai_mi_event': 'progress'})
            async with aclosing(self.events(user_query, native_config, kwargs)) as events:
                async for event in events:
                    response = event['response'] if event['event'] == 'result' else event
                    yield AIMessage(content=json.dumps(response, ensure_ascii=False),
                                    additional_kwargs={'ai_mi_event': event['event']})
        finally:
            post_tool_setup(token, kwargs=kwargs)

    async def events(self, user_query, native_config, kwargs):
        from .memory import native_memory, root_graph_config
        if not kwargs.get('subject_id'):
            raise ValueError('Authenticated AIDP subject is required')
        scope = Scope(self.config['tenant_id'], hashlib.sha256(str(kwargs['subject_id']).encode()).hexdigest(), self.config['generation_id'])
        journal = Journal(ObjectSnapshot(self.storage, self.config['namespace'], self.config['bucket'], scope), self.config['budget_microusd'])
        flow = MarketWorkflow(journal, self.model, self.provider, timezone_name=self.config['timezone'],
            run_cap_microusd=self.config['run_cap_microusd'], step_cap_microusd=self.config['step_cap_microusd'],
            usage_cost_verified=self.config['usage_cost_verified'], project=lambda records: self.project(journal))
        graph = flow.build(checkpointer=native_memory(scope.generation_id, 'ai_market_intelligence'))
        if not self.config['research_enabled']:
            mcp_verified = await self.verify_mcp()
            yield {'event': 'result', 'response': {'mode': 'configured_paused', 'streaming': 'graph_milestones',
                'generation_id': self.config['generation_id'],
                'mcp_verified': mcp_verified,
                'node_names': sorted(graph.get_graph().nodes), 'research_enabled': False, 'apify_starts': 0, 'model_calls': 0}}
            return
        config = root_graph_config(native_config)
        session = config.get('configurable', {}).get('thread_id')
        if not isinstance(session, str) or not session:
            raise ValueError('An authenticated native session is required')
        config['configurable']['thread_id'] = digest([scope.__dict__, session])
        request = await chat_request(user_query, kwargs.get('trace_id'), graph, config)
        if set(request) == {'read_notification'}:
            report = flow.proposals.read_notification(request['read_notification'])
            await self.project(journal)
            yield {'event': 'result', 'response': flow.report_response(report)}
            return
        scheduled = set(request) == {'schedule_id'}
        if scheduled:
            run_id = flow.proposals.claim_due(request['schedule_id'], resume_pending=True)
            if not run_id:
                yield {'event': 'result', 'response': {'markdown': 'La programación aún no vence o no está activa; no se inició una investigación.'}}
                return
            request = flow.scheduled_input(run_id)
        elif set(request) != {'request_id', 'question'}:
            raise ValueError('The authenticated chat adapter must supply request_id and question')
        if scheduled:
            config['configurable']['thread_id'] = digest([scope.__dict__, 'scheduled:' + run_id])
        config['recursion_limit'] = 100
        try:
            async with aclosing(stream_turn(graph, request, config)) as events:
                async for event in events:
                    if event['event'] == 'result':
                        yield {'event': 'result', 'response': event['state']['response']}
                    else:
                        yield event
        except ProviderPending:
            yield {'event': 'result', 'response': {'state': 'pending', 'request_id': request['request_id'],
                'markdown': 'La captura sigue en curso. Reanuda esta solicitud; no se iniciará otro Actor.'}}
