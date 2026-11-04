"""AIDP setup/invoke binding; native identity and memory remain authoritative."""
from contextlib import aclosing
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from . import APIFY_CREDENTIAL_NAME
from .model import DecisionModel
from .provider import ApifyProvider
from .repository import Scope, ObjectSnapshot, Journal, digest, projection_statements, projection_checks, projection_batch_check, projection_delta, ProjectionPending
from .workflow import MarketWorkflow
from .execution import ProviderPending, ProviderStatusUnavailable, ProviderReceiptUnverified, ScheduleScopeError
from .streaming import run_turn, stream_turn, TurnIncomplete, delivery_not_verified
from .presentation import response_markdown
from .contracts import RuntimeBinding
from .research import ResearchRevisionMismatch
from .connectors import PROFILES
from .catalog import PUBLIC_SEARCH_POLICIES
from .schema import schema_sha256


def runtime_source_hashes(source_path, config):
    """Separate the actual standalone program's semantic revision from injected configuration."""
    import ast
    source = Path(source_path).read_bytes()
    tree = ast.parse(source)
    target = ast.dump(ast.Name(id='RUNTIME_CONFIG', ctx=ast.Store()))
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and [ast.dump(name) for name in node.targets] == [target]]
    if len(assignments) != 1 or ast.literal_eval(assignments[0].value) != config:
        raise RuntimeError('The executed standalone source does not match its runtime configuration')
    assignments[0].value = ast.Constant(value=None)
    return digest(ast.dump(tree, include_attributes=False)), hashlib.sha256(source).hexdigest()


def build_runtime_binding(source_path, config, prompts, mcp_contract, model_settings):
    from importlib import metadata
    implementation, deployed = runtime_source_hashes(source_path, config)
    dependencies = {}
    for package in ('aidp-utils', 'langgraph', 'langchain-core', 'mcp'):
        try:
            dependencies[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            dependencies[package] = None
    return RuntimeBinding.model_validate({'version': 1, 'implementation_sha256': implementation,
        'deployed_file_sha256': deployed, 'runtime_config_sha256': digest(config),
        'prompts_sha256': digest(prompts), 'policy_sha256': digest([PROFILES, PUBLIC_SEARCH_POLICIES, mcp_contract, schema_sha256()]),
        'model_sha256': digest(model_settings), 'model_id': model_settings['model_id'], 'dependencies': dependencies}).model_dump(mode='json')


def pending_capture_response(error, request_id):
    if isinstance(error, ProviderStatusUnavailable):
        message = 'No pude consultar el estado de la búsqueda. El trabajo ya guardado se conserva. Puedes retomar esta misma solicitud; no se iniciará otra captura.'
        reason = 'status_unavailable'
    elif isinstance(error, ProviderReceiptUnverified):
        message = 'No pude confirmar que la respuesta de la fuente corresponda a esta búsqueda. El trabajo ya guardado se conserva. Hay que revisar esa respuesta antes de retomar la misma solicitud; no reinicié la captura.'
        reason = 'receipt_unverified'
    else:
        message = 'La búsqueda sigue en curso. Puedes retomar esta solicitud; no se repetirá la captura.'
        reason = 'running'
    return {'state': 'pending', 'request_id': request_id, 'pending_reason': reason, 'markdown': message}


def pending_delivery_response(error, request_id):
    message = ('Otra de tus solicitudes está terminando de guardar la información.' if error.reason == 'busy'
               else 'Otra solicitud actualizó tus registros mientras se verificaba esta respuesta.')
    return {'state': 'pending', 'request_id': request_id, 'pending_reason': 'delivery_' + error.reason,
            'markdown': message + ' Tu consulta se conserva. Vuelve a enviar esta misma pregunta cuando termine; '
                'usaré lo ya procesado sin repetir las búsquedas. La entrega todavía no está verificada.'}


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


def parse_chat_query(user_query):
    """Validate text or an explicit command before touching a checkpoint."""
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
    return question


async def chat_request(user_query, trace_id, graph, config):
    """Keep explicit command IDs; bind ordinary chat to its native invocation."""
    question = parse_chat_query(user_query)
    if isinstance(question, dict):
        return question
    session = config.get('configurable', {}).get('thread_id')
    if not isinstance(session, str) or not session.strip() or not isinstance(trace_id, str) or not 1 <= len(trace_id.strip()) <= 128:
        raise ValueError('An authenticated native session and invocation ID are required')
    previous = await graph.aget_state(config)
    values = previous.values or {}
    incomplete_delivery = delivery_not_verified(values) and ('delivery_verified_for' in values or values.get('question') == question)
    if previous.next or incomplete_delivery:
        if values.get('question') != question or not values.get('request_id'):
            raise RuntimeError('Resume the interrupted request before starting a new turn')
        return {'request_id': values['request_id'], 'question': question}
    return {'request_id': 'native:' + digest([session, trace_id]), 'question': question}


async def project_writes(writes, execute, notify):
    """Drain independent table writes before readback or releasing their lease."""
    import asyncio
    semaphore = asyncio.Semaphore(2)
    completed = 0
    async def merge(slot, statement):
        nonlocal completed
        async with semaphore:
            await execute(statement, slot=slot)
            completed += 1
            notify(completed)
    pending = asyncio.gather(*(merge(slot, statement) for slot, statement in writes), return_exceptions=True)
    cancelled = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError()
    for result in pending.result():
        if isinstance(result, BaseException):
            raise result


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
        model_settings = {'model_id': config['model_id'],
            'model_kwargs': {'temperature': 0, 'max_tokens': 6000, 'reasoning_effort': 'LOW'},
            'guardrails_config': config['guardrails_config']}
        self.model = DecisionModel(GenAIChatInvoker(provider='generic', auth_type='API_KEY',
            client=GenerativeAiInferenceV2Client(endpoint=config['model_endpoint'], signer=signer),
            is_stream=False, compartment_id=config['compartment_id'], service_endpoint=config['model_endpoint'],
            **model_settings))
        self.runtime_binding = build_runtime_binding(__file__, config, self.model.prompts, contract, model_settings)

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
        from langgraph.config import get_stream_writer
        try:
            progress = get_stream_writer()
        except RuntimeError:
            progress = lambda event: None
        async def execute(statement, *, slot):
            tool_name = 'ai_mi_projection_' + digest([journal.scope.__dict__, slot])[:32]
            tool = create_langgraph_tool(AIDPToolConf(name=tool_name, description='Project validated owner-scoped research records',
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
        records, previous = journal.claim_projection(lease)
        verified, receipts = False, None
        try:
            row_keys, receipts = projection_delta(records, config['catalog'], config['framework_schema'], config['gold_query_compute_id'], previous)
            writes = list(projection_statements(records, config['catalog'], config['framework_schema'], row_keys=row_keys))
            checks = list(projection_checks(records, config['catalog'], config['framework_schema']))
            if len(writes) != sum(bool(keys) for keys in row_keys.values()) or len(checks) != sum(bool(rows) for rows in records.tables.values()):
                raise RuntimeError('Projection rendering omitted a table verification')
            writes = list(enumerate(writes))
            batch = projection_batch_check(checks) if checks else None
            total = len(writes) + bool(batch)
            def notify(completed):
                progress({'stage': 'project_records', 'completed_steps': completed,
                          'total_steps': total})
            notify(0)
            await project_writes(writes, execute, notify)
            if batch:
                statement, expected = batch
                verify_projection(await execute(statement, slot='readback'), expected)
                notify(total)
            verified = True
        finally:
            current = journal.finish_projection(lease, records, receipts if verified else None)
        if not current:
            raise ProjectionPending('changed')

    async def notification_response(self, flow, journal, notification_id):
        report = flow.proposals.read_notification(notification_id)
        try:
            await self.project(journal)
        except ProjectionPending as error:
            return pending_delivery_response(error, notification_id)
        return flow.report_response(report)

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
            yield AIMessage(content='Revisando la solicitud.\n\n', additional_kwargs={'ai_mi_event': 'progress'})
            async with aclosing(self.events(user_query, native_config, kwargs)) as events:
                async for event in events:
                    if event['event'] == 'result':
                        response = event['response']
                        yield AIMessage(content=response_markdown(response),
                                        additional_kwargs={'ai_mi_event': 'result', 'ai_mi_response': response})
                    else:
                        yield AIMessage(content=event['markdown'] + '\n\n',
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
            usage_cost_verified=self.config['usage_cost_verified'], public_discovery=self.config['research_enabled'],
            runtime_binding=self.runtime_binding,
            project=lambda records: self.project(journal))
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
            response = await self.notification_response(flow, journal, request['read_notification'])
            yield {'event': 'result', 'response': response}
            return
        scheduled = set(request) == {'schedule_id'}
        if scheduled:
            try:
                run_id = flow.proposals.claim_due(request['schedule_id'], resume_pending=True)
            except (ScheduleScopeError, ResearchRevisionMismatch) as error:
                yield {'event': 'result', 'response': {'markdown': str(error), 'schedule_needs_review': True}}
                return
            if not run_id:
                yield {'event': 'result', 'response': {'markdown': 'La programación aún no vence o no está activa; no se inició una investigación.'}}
                return
            request = flow.scheduled_input(run_id)
        elif set(request) != {'request_id', 'question'}:
            raise ValueError('The authenticated chat adapter must supply request_id and question')
        if scheduled:
            config['configurable']['thread_id'] = digest([scope.__dict__, 'scheduled:' + run_id])
        config['recursion_limit'] = 100
        async with aclosing(self.research_events(flow, graph, request, config)) as events:
            async for event in events:
                yield event

    async def research_events(self, flow, graph, request, config):
        try:
            async with aclosing(stream_turn(graph, request, config)) as events:
                async for event in events:
                    if event['event'] == 'result':
                        yield {'event': 'result', 'response': await flow.response_for_delivery(event['state'])}
                    else:
                        yield event
        except ProviderPending as error:
            yield {'event': 'result', 'response': pending_capture_response(error, request['request_id'])}
        except ResearchRevisionMismatch as error:
            yield {'event': 'result', 'response': {'markdown': str(error), 'revision_needs_review': True}}
        except ProjectionPending as error:
            yield {'event': 'result', 'response': pending_delivery_response(error, request['request_id'])}
        except TurnIncomplete:
            yield {'event': 'result', 'response': {'state': 'pending', 'pending_reason': 'turn_incomplete',
                'request_id': request['request_id'], 'markdown': 'La consulta se interrumpió antes de completar la entrega. '
                    'El trabajo guardado se conserva. Vuelve a enviar esta misma pregunta para continuar; '
                    'no repetiré las búsquedas ya realizadas.'}}
