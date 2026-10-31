import copy
import io
from types import SimpleNamespace

import pytest

from app.labs.ai_market_intelligence.repository import (
    Conflict, Journal, ObjectSnapshot, Records, Scope, projection_statements)


SCOPE = Scope('tenant-a', 'owner-a', '00000000-0000-4000-8000-000000000001')


class MemorySnapshot:
    scope = SCOPE

    def __init__(self):
        self.value = None
        self.revision = 0

    def read(self):
        return copy.deepcopy(self.value), self.revision or None

    def write(self, value, etag):
        if etag != (self.revision or None):
            raise Conflict('changed')
        self.value = copy.deepcopy(value)
        self.revision += 1


def test_records_reject_cross_owner_and_missing_references():
    records = Records(SCOPE)
    with pytest.raises(ValueError, match='Cross-owner'):
        records.upsert('ai_mi_topics', {'topic_id': SCOPE.key('t1'), 'owner_id': 'someone-else'})
    with pytest.raises(ValueError, match='Missing scoped reference'):
        records.upsert('ai_mi_questions', {'question_id': SCOPE.key('q1'), 'topic_id': 'missing'})
    assert records.rows('ai_mi_questions') == []


def test_journal_persists_related_rows_as_one_conditional_revision():
    journal = Journal(MemorySnapshot(), 0)

    def write(records, state):
        records.upsert('ai_mi_topics', {'topic_id': SCOPE.key('t1')})
        records.upsert('ai_mi_questions', {'question_id': SCOPE.key('q1'), 'topic_id': SCOPE.key('t1')})

    journal.transact(write)
    value, _ = journal.read()
    assert value['revision'] == 1
    assert value['tables']['ai_mi_questions'][SCOPE.key('q1')]['topic_id'] == SCOPE.key('t1')


def test_failed_transaction_does_not_persist_partial_rows():
    journal = Journal(MemorySnapshot(), 0)

    def fail(records, state):
        records.upsert('ai_mi_topics', {'topic_id': SCOPE.key('t1')})
        raise RuntimeError('failed before commit')

    with pytest.raises(RuntimeError):
        journal.transact(fail)
    assert journal.read()[0]['tables']['ai_mi_topics'] == {}


def test_conditional_conflict_retries_only_local_change():
    snapshot = MemorySnapshot()
    original = snapshot.write
    calls = []

    def conflict_once(value, etag):
        calls.append(value['revision'])
        if len(calls) == 1:
            raise Conflict('concurrent update')
        original(value, etag)

    snapshot.write = conflict_once
    journal = Journal(snapshot, 0)
    journal.transact(lambda records, state: records.upsert('ai_mi_topics', {'topic_id': SCOPE.key('t1')}))
    assert calls == [1, 1]
    assert journal.read()[0]['revision'] == 1


def test_projection_is_scoped_and_does_not_embed_untrusted_sql():
    records = Records(SCOPE)
    text = "'); DROP TABLE unrelated; --"
    records.upsert('ai_mi_questions', {'question_id': SCOPE.key('q1'), 'question_text': text})
    sql, = projection_statements(records, 'catalog', 'oci_artifacts')
    assert text not in sql and text.encode().hex() in sql
    assert '`tenant_id`=s.`tenant_id`' in sql and '`owner_id`=s.`owner_id`' in sql
    assert 'MERGE INTO `catalog`.`oci_artifacts`.`ai_mi_questions`' in sql


def test_object_snapshot_closes_body_and_uses_conditional_write():
    calls = []
    body = SimpleNamespace(raw=io.BytesIO(b'{"version":1}'), close=lambda: calls.append('closed'))
    storage = SimpleNamespace(get_object=lambda *args: SimpleNamespace(data=body, headers={'etag': 'v1'}),
                              put_object=lambda *args, **kwargs: calls.append(kwargs))
    snapshot = ObjectSnapshot(storage, 'ns', 'bucket', SCOPE)
    assert snapshot.read() == ({'version': 1}, 'v1')
    snapshot.write({'version': 2}, 'v1')
    assert calls[0] == 'closed' and calls[1]['if_match'] == 'v1'


def test_bound_budget_cannot_be_silently_changed():
    snapshot = MemorySnapshot()
    Journal(snapshot, 100).transact(lambda records, state: None)
    with pytest.raises(RuntimeError, match='binding changed'):
        Journal(snapshot, 200).read()


@pytest.mark.parametrize('value', ['2026-10-30T09:00:00', '2026-10-30T09:00:00-05:00'])
def test_timestamps_require_explicit_utc(value):
    with pytest.raises(ValueError, match='UTC'):
        Records(SCOPE).upsert('ai_mi_topics', {'topic_id': SCOPE.key('t1'), 'created_at': value})


def test_persisted_empty_table_map_is_rejected():
    with pytest.raises(ValueError, match='table set'):
        Records(SCOPE, {})


def test_loaded_snapshot_checks_all_references():
    records = Records(SCOPE)
    records.upsert('ai_mi_questions', {'question_id': SCOPE.key('q1')})
    records.tables['ai_mi_questions'][SCOPE.key('q1')]['topic_id'] = SCOPE.key('missing')
    with pytest.raises(ValueError, match='Missing scoped reference'):
        Records(SCOPE, records.tables)


def test_generation_cannot_overwrite_another_generations_identity():
    other = Scope('tenant-a', 'owner-a', '00000000-0000-4000-8000-000000000002')
    assert SCOPE.key('same-topic') != other.key('same-topic')
    with pytest.raises(ValueError, match='bound generation'):
        Records(other).upsert('ai_mi_topics', {'topic_id': SCOPE.key('same-topic')})
