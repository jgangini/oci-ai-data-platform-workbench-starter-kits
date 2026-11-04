"""Stream real graph milestones without exposing model decisions or tool payloads."""


class TurnIncomplete(RuntimeError):
    pass


def delivery_not_verified(values):
    return bool(values.get('response')) and values.get('delivery_verified_for') != values.get('request_id')


PROGRESS = {
    'bind_request': 'Solicitud validada.',
    'understand': 'Consulta interpretada.',
    'resolve_context': 'Fuentes registradas y periodo revisados.',
    'prepare_run': 'Investigación preparada.',
    'admit_step': 'Alcance y presupuesto del paso evaluados.',
    'execute_step': 'Paso de captura procesado; conciliando su estado.',
    'reconcile_step': 'Conciliación de captura evaluada.',
    'normalize_evidence': 'Evidencia del paso revisada.',
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
    verified = values.get('delivery_verified_for') == request['request_id']
    if same_request and not previous.next and verified:
        yield {'event': 'result', 'state': values}
        return
    incomplete_delivery = 'delivery_verified_for' in values and delivery_not_verified(values)
    if (previous.next or incomplete_delivery) and not same_request:
        raise RuntimeError('Resume the interrupted request before starting a new turn')
    graph_input = None if same_request else {'schedule_run_id': None, **request}
    if same_request and not previous.next:
        if not values.get('response'):
            raise TurnIncomplete('The interrupted request has no prepared delivery to verify')
        from langgraph.types import Command
        graph_input = Command(goto='deliver_and_read')
    stream = graph.astream(graph_input, config=config, stream_mode=['updates', 'values', 'custom'])
    result = None
    pending_stages = []
    try:
        async for mode, data in stream:
            if mode == 'values':
                result = data
                # Pair node milestones with updated state, including custom streams.
                for stage in pending_stages:
                    if stage in PROGRESS:
                        yield {'event': 'progress', 'stage': stage, 'markdown': PROGRESS[stage]}
                pending_stages.clear()
            elif mode == 'custom' and isinstance(data, dict) and data.get('stage') == 'project_records':
                completed, total = data.get('completed_steps'), data.get('total_steps')
                if type(completed) is int and type(total) is int and 0 <= completed <= total <= 28:
                    yield {'event': 'progress', 'stage': 'project_records',
                           'markdown': f'Guardando y verificando registros: {completed} de {total} pasos.'}
            elif mode == 'updates':
                pending_stages.extend(data)
    finally:
        # Disconnects must close this execution, never leave an unobserved producer.
        await stream.aclose()
    if result is None or not result.get('response'):
        raise RuntimeError('Research ended without a verified response')
    completed = await graph.aget_state(config)
    if (completed.next or completed.values.get('request_id') != request['request_id']
            or completed.values.get('delivery_verified_for') != request['request_id']
            or result.get('delivery_verified_for') != request['request_id']
            or completed.values.get('response') != result['response']):
        raise TurnIncomplete('The graph checkpoint does not confirm this completed delivery')
    yield {'event': 'result', 'state': result}


async def run_turn(graph, request, config):
    async for event in stream_turn(graph, request, config):
        if event['event'] == 'result':
            return event['state']
