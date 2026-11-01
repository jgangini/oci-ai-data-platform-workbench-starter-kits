"""MCP transport is reachable only through the admitted operation ledger."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from .mcp_payload import unpack, unpack_run, locate, dataset_id
from .catalog import public_url


def _run_records(value):
    """A receipt must bind run identity and status in the same provider object."""
    if isinstance(value, dict):
        identity = value.get('runId') or value.get('actorRunId') or value.get('id')
        if isinstance(identity, str) and identity and value.get('status') in {'READY', 'RUNNING', 'SUCCEEDED', 'FAILED', 'ABORTED', 'TIMED-OUT'}:
            yield value
            return
        for child in value.values():
            yield from _run_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from _run_records(child)


class ApifyProvider:
    def __init__(self, tools):
        required = {'call-actor', 'get-actor-run', 'get-dataset-items'}
        if not required.issubset(tools):
            raise ValueError('Required MCP capabilities are missing')
        self.tools = tools

    async def invoke(self, name, arguments):
        raw = await self.tools[name].ainvoke(arguments)
        if getattr(raw, 'isError', False) or isinstance(raw, dict) and (raw.get('isError') or raw.get('error')):
            raise RuntimeError('MCP returned a provider error')
        value = unpack_run(raw) if name in {'call-actor', 'get-actor-run'} else unpack(raw)
        if isinstance(value, dict) and value.get('error'):
            raise RuntimeError('MCP returned an error envelope')
        return value

    @staticmethod
    def receipt(value, expected_id=None):
        meta = value.get('_meta', {}) if isinstance(value, dict) else {}
        usage = meta.get('com.apify/ActorRun') if isinstance(meta, dict) else None
        payload = value['payload'] if isinstance(value, dict) and '_meta' in value and 'payload' in value else value
        records = list(_run_records(payload))
        matches = [item for item in records
                   if expected_id is None or (item.get('runId') or item.get('actorRunId') or item['id']) == expected_id]
        if len(matches) != 1:
            raise ValueError('Provider run identity and status could not be verified together')
        run = matches[0]
        identity = run.get('runId') or run.get('actorRunId') or run['id']
        cost = _run_cost(run, usage, identity, len(records))
        if cost is not None:
            cost = int((cost * 1000000).to_integral_value(rounding=ROUND_CEILING))
        return {'runId': identity,
                'status': run['status'], 'datasetId': dataset_id(run), 'spent_microusd': cost}

    async def start(self, call):
        return self.receipt(await self.invoke('call-actor', call))

    async def read(self, run_id):
        return self.receipt(await self.invoke('get-actor-run', {'runId': run_id, 'waitSecs': 0}), run_id)

    async def observations(self, receipt, call):
        if not receipt.get('datasetId'):
            receipt = await self.read(receipt['runId'])
        if receipt['status'] != 'SUCCEEDED' or not receipt.get('datasetId'):
            raise RuntimeError('The successful run has no verified dataset')
        values = await self.invoke('get-dataset-items', {'datasetId': receipt['datasetId'], 'offset': 0, 'limit': 50, 'clean': True})
        rows = _items(values)
        return [_observation(row, call['actor'], receipt['runId']) for row in rows[:50]], receipt.get('spent_microusd')


def _run_cost(run, usage, identity, run_count):
    """Reconcile total-cost channels only after the payload run is identified."""
    cost = _cost_amount(run.get('usageTotalUsd'))
    if usage is None:
        return cost
    if not isinstance(usage, dict) or run_count != 1:
        raise ValueError('Provider usage metadata has no unique run')
    if any(usage[key] != identity for key in ('runId', 'actorRunId', 'id') if key in usage):
        raise ValueError('Provider usage metadata belongs to another run')
    metadata_cost = _cost_amount(usage.get('usageTotalUsd'))
    if metadata_cost is None:
        return cost
    if cost not in (None, metadata_cost):
        raise ValueError('Provider run cost disagrees with its usage metadata')
    return metadata_cost


def _cost_amount(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('Provider cost is not a finite nonnegative amount')
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Provider cost is not a finite nonnegative amount') from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError('Provider cost is not a finite nonnegative amount')
    return amount


def _items(value):
    if isinstance(value, list):
        if len(value) == 1 and isinstance(value[0], dict) and any(key in value[0] for key in ('items', 'data', 'datasetItems')):
            return _items(value[0])
        if not all(isinstance(row, dict) for row in value):
            raise ValueError('Dataset items must be objects')
        return value
    if isinstance(value, dict):
        for name in ('items', 'data', 'datasetItems'):
            if name in value:
                return _items(value[name])
    raise ValueError('Dataset response has no verified item list')


def _observation(row, actor, run_id):
    url = public_url(row.get('url') or row.get('twitterUrl'))
    text = row.get('markdown') or row.get('text') or row.get('fullText')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Provider item has no textual evidence')
    published = row.get('createdAt') or row.get('publishedAt')
    if published:
        try:
            moment = datetime.fromisoformat(published.replace('Z', '+00:00'))
        except ValueError:
            moment = datetime.strptime(published, '%a %b %d %H:%M:%S %z %Y')
        if moment.tzinfo is None:
            raise ValueError('Provider date lacks a timezone')
        published = moment.astimezone(timezone.utc).isoformat()
    return {'url': url, 'text': text[:12000], 'published_at': published,
            'observed_at': datetime.now(timezone.utc).isoformat(), 'metrics': {},
            'provenance': {'source': actor, 'provider_run_id': run_id, 'scope': 'bounded_dataset_sample', 'location': None}}
