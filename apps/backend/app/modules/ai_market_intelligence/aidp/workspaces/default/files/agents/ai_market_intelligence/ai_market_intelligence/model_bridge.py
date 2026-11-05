"""Verified structured OCI adapter for current local evaluations."""
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
import time
from uuid import uuid4


def update_model_observation(path, record, state, **values):
    record.update(state=state, **values)
    record['history'].append({'state': state, 'at': datetime.now(timezone.utc).isoformat()})
    temporary = path.with_suffix('.pending')
    raw = json.dumps(record, ensure_ascii=False)
    for attempt in range(5):
        try:
            temporary.write_text(raw, encoding='utf-8')
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02)


def model_error_metadata(error):
    metadata = {'error_chain': []}
    cause = error
    while cause is not None and len(metadata['error_chain']) < 5:
        metadata['error_chain'].append(type(cause).__name__)
        status = getattr(cause, 'status', None)
        if type(status) is int and 100 <= status <= 599:
            metadata['provider_status'] = status
        cause = cause.__cause__ or cause.__context__
    return metadata


class StructuredBridge:
    """Use the worker's counted call and final-response validation with strict JSON."""
    def __init__(self, tracked, binding, *, observations_dir=None):
        self.tracked, self.binding = tracked, binding
        self.observations_dir, self.observation_id, self.sequence = observations_dir, uuid4().hex, 0
        self.schema = ContextVar('market_decision_schema')
        self.observation = ContextVar('market_model_observation', default=None)
        if hasattr(tracked, '_complete'):
            # Only the request construction differs; complete() still owns admission,
            # counting, errors and _final_text() validates the actual OCI response.
            tracked._complete = self.request

    def request(self, system, question):
        import oci
        from oci.generative_ai_inference import models
        tracked, settings = self.tracked, self.tracked.settings
        if tracked.client is None:
            config = oci.config.from_file(tracked.config_file, tracked.profile)
            identity = hashlib.sha256(json.dumps([config[key] for key in ('tenancy', 'user', 'fingerprint')],
                separators=(',', ':')).encode()).hexdigest()
            if identity != self.binding['identity_sha256'] or self.binding['guardrails_config'] != {'policies': []}:
                raise ValueError('Local adapter identity or guardrail parity is not verified')
            tracked.client = oci.generative_ai_inference.GenerativeAiInferenceClient(
                {**config, 'region': settings['region']}, service_endpoint=self.binding['model_endpoint'],
                retry_strategy=oci.retry.NoneRetryStrategy(), timeout=(10, 120))
        request = models.GenericChatRequest(temperature=0, max_tokens=settings['max_tokens'],
            reasoning_effort=settings['reasoning_effort'], is_stream=False,
            messages=[models.SystemMessage(content=[models.TextContent(text=system)]),
                      models.UserMessage(content=[models.TextContent(text=question)])],
            response_format=models.JsonSchemaResponseFormat(json_schema=models.ResponseJsonSchema(
                name='ai_mi_decision', schema=self.schema.get(), is_strict=True)))
        observation = self.observation.get()
        options = {'retry_strategy':oci.retry.NoneRetryStrategy()}
        if observation is not None:
            path, record = observation
            update_model_observation(path, record, 'HTTP_SUBMISSION_INTENT')
            options['opc_request_id'] = record['client_request_id']
        response = tracked.client.chat(models.ChatDetails(compartment_id=settings['compartment_id'],
            serving_mode=models.OnDemandServingMode(model_id=settings['model_id']), chat_request=request), **options)
        if observation is not None:
            update_model_observation(path, record, 'HTTP_RESPONSE_RECEIVED',
                provider_request_id=(getattr(response, 'headers', None) or {}).get('opc-request-id'))
        return tracked._final_text(response.data.chat_response)

    def with_structured_output(self, schema, method):
        if method != 'json_schema':
            raise ValueError('Only the production structured-output contract is supported')
        parent = self

        class Bound:
            async def ainvoke(self, messages):
                if len(messages) != 2 or [message.type for message in messages] != ['system', 'human']:
                    raise ValueError('Unexpected production decision message contract')
                token = parent.schema.set(schema)
                observation_token = None
                path, record = None, None
                try:
                    if parent.observations_dir is not None:
                        parent.observations_dir.mkdir(parents=True, exist_ok=True)
                        parent.sequence += 1
                        path = parent.observations_dir / f'{parent.observation_id}-{parent.sequence}.json'
                        record = {'input_sha256': hashlib.sha256(messages[1].content.encode()).hexdigest(),
                            'schema_sha256': hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
                            'client_request_id': uuid4().hex, 'worker_pid': os.getpid(), 'history': []}
                        with path.open('x', encoding='utf-8') as output:
                            json.dump(record, output, ensure_ascii=False)
                        update_model_observation(path, record, 'CALL_ADMITTED')
                        observation_token = parent.observation.set((path, record))
                    raw = await parent.tracked.complete(messages[0].content, messages[1].content)
                    if path is not None:
                        update_model_observation(path, record, 'COMPLETE_RESPONSE', response=raw)
                    return json.loads(raw)
                except Exception as error:
                    if path is not None:
                        update_model_observation(path, record,
                            'LOCAL_VALIDATION_FAILED' if 'response' in record else 'FAILED_UNKNOWN_USAGE',
                            error_type=type(error).__name__, **model_error_metadata(error))
                    raise
                finally:
                    if observation_token is not None:
                        parent.observation.reset(observation_token)
                    parent.schema.reset(token)

        return Bound()


