"""Stream real graph milestones without exposing model decisions or tool payloads."""


PROGRESS = {
    'bind_request': 'Solicitud validada.',
    'understand': 'Consulta interpretada.',
    'resolve_context': 'Fuentes registradas y periodo revisados.',
    'prepare_run': 'Investigación preparada.',
    'admit_step': 'Alcance y presupuesto del paso evaluados.',
    'execute_step': 'Paso de captura procesado; conciliando su estado.',
    'reconcile_step': 'Conciliación de captura evaluada.',
    'normalize_evidence': 'Evidencia recopilada.',
    'assess_evidence': 'Evidencia contrastada.',
    'draft_report': 'Borrador preparado; verificando la entrega.',
    'publish_result': 'Informe registrado; verificando su persistencia.',
    'handle_confirmation': 'Respuesta procesada.',
}


async def stream_turn(graph, request, config):
    previous = await graph.aget_state(config)
    values = previous.values or {}
    same_request = values.get('request_id') == request['request_id']
    if same_request and values.get('question') != request['question']:
        raise ValueError('Request ID was reused for another question')
    if same_request and not previous.next:
        yield {'event': 'result', 'state': values}
        return
    if previous.next and not same_request:
        raise RuntimeError('Resume the interrupted request before starting a new turn')
    graph_input = None if same_request else {'schedule_run_id': None, **request}
    stream = graph.astream(graph_input, config=config, stream_mode=['updates', 'values'])
    result = None
    try:
        async for mode, data in stream:
            if mode == 'values':
                result = data
            elif mode == 'updates':
                for stage in data:
                    if stage in PROGRESS:
                        yield {'event': 'progress', 'stage': stage, 'markdown': PROGRESS[stage]}
    finally:
        # Disconnects must close this execution, never leave an unobserved producer.
        await stream.aclose()
    if result is None or not result.get('response'):
        raise RuntimeError('Research ended without a verified response')
    yield {'event': 'result', 'state': result}


async def run_turn(graph, request, config):
    async for event in stream_turn(graph, request, config):
        if event['event'] == 'result':
            return event['state']
