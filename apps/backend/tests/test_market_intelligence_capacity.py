"""Capacity failures preserve paid receipts and must not deny an earlier start."""
import asyncio
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.execution import execute_selected
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import ObjectSnapshot, encoded, json_text, utc_now
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.research import Research
from test_market_intelligence_execution import prepared


class ObjectStorage:
    def __init__(self, value):
        self.body, self.revision, self.writes = encoded(value), 1, 0

    def get_object(self, *args):
        return SimpleNamespace(data=SimpleNamespace(raw=io.BytesIO(self.body), close=lambda: None),
                               headers={'etag': str(self.revision)})

    def put_object(self, namespace, bucket, key, body, **kwargs):
        assert kwargs['if_match'] == str(self.revision)
        self.body = body
        self.revision += 1
        self.writes += 1


def bounded_ledger(headroom):
    ledger, run, source = prepared()
    now = datetime.now(timezone.utc)
    ledger.journal.transact(lambda records, state: records.upsert('ai_mi_research_runs', {
        **records.get('ai_mi_research_runs', run),
        'plan_window_json': json_text({'start': (now - timedelta(days=1)).isoformat(), 'end': now.isoformat()})}))
    value, _ = ledger.journal.read()
    storage = ObjectStorage(value)
    snapshot = ObjectSnapshot(storage, 'namespace', 'bucket', ledger.journal.scope)
    snapshot.maximum_bytes = len(storage.body) + headroom
    ledger.journal.snapshot = snapshot
    return ledger, run, source, storage


def test_capacity_exhaustion_before_reservation_does_not_start_the_provider():
    ledger, run, source, storage = bounded_ledger(0)
    before, _ = ledger.journal.read()
    with pytest.raises(RuntimeError, match='capacity exceeded'):
        ledger.reserve(run, source, None, {'actor': 'reviewed-x'}, 10000, 'Find current mentions',
            authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    assert ledger.journal.read()[0] == before
    assert storage.writes == 0 and not ledger.journal.read()[0]['operations']


def test_capacity_exhaustion_after_a_paid_start_retains_receipt_and_never_restarts():
    ledger, run, source, storage = bounded_ledger(20000)
    call = {'actor': 'reviewed-x'}
    tool = ledger.reserve(run, source, None, call, 10000, 'Find current mentions',
        authorization_hash=ledger.authorize(run, source, None)['authorization_hash'])
    starts = []
    async def start(arguments):
        starts.append(arguments)
        return {'runId': 'already-started-run', 'status': 'SUCCEEDED', 'spent_microusd': 1000}
    asyncio.run(execute_selected(ledger, tool, call, start))
    before, _ = ledger.journal.read()
    writes = storage.writes
    with pytest.raises(RuntimeError, match='capacity exceeded') as failure:
        Research(ledger.journal).add_evidence(run, tool, [{'url': 'https://example.org/evidence',
            'text': '\U0001f600' * 12000, 'published_at': None, 'observed_at': utc_now(), 'metrics': {},
            'provenance': {'source': 'reviewed-x', 'provider_run_id': 'already-started-run'}}])
    assert 'no paid start admitted' not in str(failure.value)
    assert 'Reconcile' in str(failure.value)
    assert ledger.journal.read()[0] == before and storage.writes == writes
    operation = before['operations'][tool]
    assert operation['provider_run_id'] == 'already-started-run'
    assert operation['state'] == 'succeeded' and operation['spent_microusd'] == 1000
    assert not before['tables']['ai_mi_documents']
    asyncio.run(execute_selected(ledger, tool, call, start))
    assert len(starts) == 1


def test_receipt_that_cannot_fit_keeps_the_start_uncertain_and_blocks_another_paid_call():
    ledger, run, source, _ = bounded_ledger(20000)
    call = {'actor': 'reviewed-x'}
    authority = ledger.authorize(run, source, None)['authorization_hash']
    tool = ledger.reserve(run, source, None, call, 10000, 'Find current mentions', authorization_hash=authority)
    starts = []
    async def start(arguments):
        starts.append(arguments)
        submitted, _ = ledger.journal.read()
        ledger.journal.snapshot.maximum_bytes = len(encoded(submitted))
        return {'runId': 'receipt-cannot-fit', 'status': 'SUCCEEDED', 'spent_microusd': 1000}
    with pytest.raises(RuntimeError, match='outcome is uncertain'):
        asyncio.run(execute_selected(ledger, tool, call, start))
    operation = ledger.journal.read()[0]['operations'][tool]
    assert operation['state'] == 'uncertain' and operation['permit']
    assert operation.get('provider_run_id') is None
    assert operation['reserved_microusd'] == 10000 and operation['spent_microusd'] is None
    assert asyncio.run(execute_selected(ledger, tool, call, start))['state'] == 'reconcile'
    with pytest.raises(RuntimeError, match='reconciled first'):
        ledger.reserve(run, source, None, {'actor': 'changed'}, 10000, 'Another purpose', authorization_hash=authority)
    assert len(starts) == 1
