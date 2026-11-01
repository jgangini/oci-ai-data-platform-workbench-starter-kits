import copy
import io
import sqlite3
from types import SimpleNamespace

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import (
    Conflict, Journal, ObjectSnapshot, Records, Scope, projection_batch_check, projection_checks, projection_statements)


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


def test_projection_batch_validates_checks_instead_of_summing_their_expected_rows():
    checks = [('SELECT 2 AS total_rows, 2 AS distinct_keys, 2 AS matched_rows', 2),
              ('SELECT 3 AS total_rows, 3 AS distinct_keys, 3 AS matched_rows', 3)]
    statement, expected = projection_batch_check(checks)
    with sqlite3.connect(':memory:') as database:
        assert database.execute(statement).fetchone() == (2, 2, 2)
    assert expected == 2


@pytest.mark.parametrize('bad_check', [
    'SELECT 1 AS total_rows, 1 AS distinct_keys, 1 AS matched_rows WHERE 0',
    'SELECT 1 AS total_rows, 1 AS distinct_keys, 1 AS matched_rows UNION ALL SELECT 1, 1, 1',
    'SELECT 0 AS total_rows, 1 AS distinct_keys, 1 AS matched_rows',
    'SELECT 1 AS total_rows, 0 AS distinct_keys, 1 AS matched_rows',
    'SELECT 1 AS total_rows, 1 AS distinct_keys, 0 AS matched_rows',
    'SELECT 1 AS total_rows, 1 AS distinct_keys, NULL AS matched_rows',
], ids=['missing-result', 'duplicate-result', 'wrong-row-count', 'duplicate-keys', 'wrong-values', 'null-values'])
def test_projection_batch_rejects_incomplete_duplicate_or_mismatched_readback(bad_check):
    checks = [('SELECT 1 AS total_rows, 1 AS distinct_keys, 1 AS matched_rows', 1), (bad_check, 1)]
    statement, expected = projection_batch_check(checks)
    with sqlite3.connect(':memory:') as database:
        result = database.execute(statement).fetchone()
    assert result != (expected, expected, expected)


def test_projection_batch_cannot_hide_compensating_count_errors_between_tables():
    checks = [('SELECT 1 AS total_rows, 1 AS distinct_keys, 1 AS matched_rows', 2),
              ('SELECT 3 AS total_rows, 3 AS distinct_keys, 3 AS matched_rows', 2)]
    statement, expected = projection_batch_check(checks)
    with sqlite3.connect(':memory:') as database:
        assert database.execute(statement).fetchone() == (2, 2, 0)
    assert expected == 2


def test_projection_batch_cannot_replace_a_missing_check_with_another_checks_duplicate():
    checks = [('SELECT 1 AS total_rows, 1 AS distinct_keys, 1 AS matched_rows WHERE 0', 1),
              ('SELECT 1 AS total_rows, 1 AS distinct_keys, 1 AS matched_rows UNION ALL SELECT 1, 1, 1', 1)]
    statement, expected = projection_batch_check(checks)
    with sqlite3.connect(':memory:') as database:
        assert database.execute(statement).fetchone() == (2, 1, 2)
    assert expected == 2


def test_projection_batch_empty_readback_produces_zero_counts_instead_of_success():
    statement, expected = projection_batch_check([
        ('SELECT 1 AS total_rows, 1 AS distinct_keys, 1 AS matched_rows WHERE 0', 1)])
    with sqlite3.connect(':memory:') as database:
        assert database.execute(statement).fetchone() == (0, 0, 0)
    assert expected == 1


def test_projection_batch_accepts_the_fourteen_table_bound_and_preserves_original_selects():
    checks = [(f'SELECT {count} AS total_rows, {count} AS distinct_keys, {count} AS matched_rows', count)
              for count in range(1, 15)]
    statement, expected = projection_batch_check(checks)
    with sqlite3.connect(':memory:') as database:
        assert database.execute(statement).fetchone() == (14, 14, 14)
    assert expected == 14
    records = Records(SCOPE)
    records.upsert('ai_mi_topics', {'topic_id': SCOPE.key('batch-topic')})
    records.upsert('ai_mi_questions', {'question_id': SCOPE.key('batch-question'), 'topic_id': SCOPE.key('batch-topic')})
    generated = list(projection_checks(records, 'catalog', 'oci_artifacts'))
    statement, expected = projection_batch_check(generated)
    assert expected == len(generated) == 2
    assert all('FROM (' + select + ')' in statement for select, _ in generated)


@pytest.mark.parametrize('checks', [None, [], [('SELECT 1', 1)] * 15, [('SELECT 1', 0)],
                                   [('SELECT 1', -1)], [('SELECT 1', True)], [('SELECT 1', 1.0)],
                                   [('', 1)], [(None, 1)], [('SELECT 1',)]])
def test_projection_batch_rejects_invalid_shape_or_nonpositive_expected_rows(checks):
    with pytest.raises(ValueError):
        projection_batch_check(checks)
