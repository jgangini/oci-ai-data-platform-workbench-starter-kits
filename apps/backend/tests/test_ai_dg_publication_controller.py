from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

import pytest

from app.ai_data_governance.publication import publish_work
from test_ai_dg_materialize import fixture as documentary_fixture, materialize
from test_ai_dg_inventory_writer import AT
from test_ai_dg_publication import NativeTable


def fixture(column=False):
    repository, journal, document, work = documentary_fixture(asset_index=int(column))
    materialize(repository, journal)
    native = NativeTable()
    native.detail['key'] = 'catalog.schema.users'
    native.detail['tableFields'][0]['fieldName'] = 'ABC13'
    repository.calls.clear()
    return repository, journal, native, work


def publish(repository, journal, native, work, **kwargs):
    return publish_work(repository, journal, native.request, 'scope', work['work_id'], 'run',
        check_active=kwargs.pop('check_active', Mock()), observed_at=kwargs.pop('observed_at', AT), **kwargs)


def only_publication(repository):
    row, = repository.data['AI_DG_PUBLICATION'].values()
    return row


def put_count(native):
    return sum(method == 'PUT' for method, *_ in native.calls)


@pytest.mark.parametrize('column', [False, True])
def test_durable_intent_and_pending_projection_precede_exact_partial_write(column):
    repository, journal, native, work = fixture(column)
    request = native.request
    def guarded(method, path, **kwargs):
        if method == 'PUT':
            row = only_publication(repository)
            assert row['catalog_state'] == 'PREPARED'
            assert journal.read('claims', row['publication_id'])
            assert repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['consistency_state'] == 'UPDATE_PENDING'
        return request(method, path, **kwargs)
    native.request = guarded
    result = publish(repository, journal, native, work)
    assert result['catalog_state'] == 'VERIFIED' and result['replica_state'] == 'PENDING'
    assert put_count(native) == 1
    assert only_publication(repository)['expected_native_version'].startswith('OBSERVATION:')
    assert repository.data['AI_DG_WORK'][(work['work_id'],)]['phase'] == 'REPLICATE'
    assert repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['consistency_state'] == 'REPLICA_PENDING'
    assert native.detail['tableFields'][1]['fieldDescription'] == 'Texto humano'
    assert publish(repository, journal, native, work, observed_at=AT + timedelta(hours=1))['write_attempted'] is False
    assert put_count(native) == 1


@pytest.mark.parametrize('text', ['Descripción humana', 'Texto anterior [IA]', '[IA]'])
def test_existing_text_is_preserved_and_not_counted_as_a_new_publication(text):
    repository, journal, native, work = fixture()
    native.detail['description'] = text
    result = publish(repository, journal, native, work)
    assert result['catalog_state'] == 'CONFLICT' and not result['write_attempted']
    assert only_publication(repository)['last_error_code'] == 'DESCRIPTION_PRESERVED'
    assert native.detail['description'] == text and put_count(native) == 0


def test_lost_claim_acknowledgement_never_starts_the_write_on_retry():
    repository, journal, native, work = fixture()
    create = journal.create
    def lost(kind, key, value):
        result = create(kind, key, value)
        if kind == 'claims':
            raise TimeoutError('claim may have committed')
        return result
    journal.create = lost
    with pytest.raises(TimeoutError):
        publish(repository, journal, native, work)
    journal.create = create
    result = publish(repository, journal, native, work)
    assert result['catalog_state'] == 'UNKNOWN'
    assert put_count(native) == 0 and native.detail['description'] is None


def test_lost_terminal_receipt_ack_recovers_without_rewriting_after_evidence_expires():
    repository, journal, native, work = fixture()
    for evidence in repository.data['AI_DG_EVIDENCE'].values():
        evidence['expires_at'] = AT + timedelta(minutes=30)
    create = journal.create
    def lost(kind, key, value):
        result = create(kind, key, value)
        if kind == 'publications':
            raise TimeoutError('receipt may have committed')
        return result
    journal.create = lost
    with pytest.raises(TimeoutError):
        publish(repository, journal, native, work)
    assert put_count(native) == 1
    journal.create = create
    assert publish(repository, journal, native, work, observed_at=AT + timedelta(hours=1))['catalog_state'] == 'VERIFIED'
    assert put_count(native) == 1
    assert only_publication(repository)['catalog_verified_at'] == AT


def test_missing_terminal_receipt_reconciles_a_committed_write_without_a_second_put():
    repository, journal, native, work = fixture()
    native.readback_error = True
    assert publish(repository, journal, native, work)['catalog_state'] == 'UNKNOWN'
    native.readback_error = False
    assert publish(repository, journal, native, work)['catalog_state'] == 'VERIFIED'
    assert put_count(native) == 1


def test_uncertain_still_blank_write_is_not_resubmitted():
    repository, journal, native, work = fixture()
    original = native.request
    def delayed(method, path, **kwargs):
        if method == 'PUT':
            native.calls.append((method, path, kwargs))
            raise TimeoutError('may still execute remotely')
        return original(method, path, **kwargs)
    native.request = delayed
    assert publish(repository, journal, native, work)['catalog_state'] == 'UNKNOWN'
    assert publish(repository, journal, native, work)['catalog_state'] == 'UNKNOWN'
    assert put_count(native) == 1


def test_evidence_expired_before_first_write_cannot_authorize_publication():
    repository, journal, native, work = fixture()
    for evidence in repository.data['AI_DG_EVIDENCE'].values():
        evidence['expires_at'] = AT
    before = deepcopy(repository.data)
    with pytest.raises(ValueError, match='expired'):
        publish(repository, journal, native, work)
    assert not native.calls
    assert not any(kind == 'claims' for kind, key in journal.rows)
    assert repository.data == before


@pytest.mark.parametrize('target', ['writer', 'scope', 'document', 'version', 'producer', 'agent'])
def test_foreign_or_unadmitted_context_cannot_write_catalog(target):
    repository, journal, native, work = fixture()
    if target == 'writer':
        repository.data['AI_DG_RUN'][('run',)]['run_kind'] = 'AGENT'
    elif target == 'scope':
        repository.data['AI_DG_SCOPE'][('scope',)]['enabled'] = False
    elif target == 'document':
        next(iter(repository.data['AI_DG_DOCUMENT'].values()))['work_id'] = 'foreign'
    elif target == 'version':
        repository.data['AI_DG_ASSET_VERSION'][(work['expected_version_id'],)]['metadata_hash'] = 'changed'
    elif target == 'producer':
        repository.data['AI_DG_RUN'][('producer',)]['scope_id'] = 'foreign'
    else:
        repository.data['AI_DG_AGENT'][('documentary',)]['workspace_key'] = 'foreign'
    with pytest.raises(ValueError):
        publish(repository, journal, native, work)
    assert not native.calls and not repository.data['AI_DG_PUBLICATION']


def test_cancellation_after_reservation_cannot_submit_a_put():
    repository, journal, native, work = fixture()
    def active():
        if any(kind == 'claims' for kind, key in journal.rows):
            raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        publish(repository, journal, native, work, check_active=active)
    assert not native.calls


def test_completed_replica_replay_does_not_mark_the_projection_pending_again():
    repository, journal, native, work = fixture()
    publish(repository, journal, native, work)
    only_publication(repository)['replica_state'] = 'VERIFIED'
    repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['consistency_state'] = 'VERIFIED'
    repository.data['AI_DG_WORK'][(work['work_id'],)].update(state='DONE')
    before = deepcopy(repository.data)
    result = publish(repository, journal, native, work)
    assert result['replica_state'] == 'VERIFIED'
    assert repository.data == before and put_count(native) == 1


def test_old_redacted_receipt_requires_fresh_read_and_never_another_write():
    repository, journal, native, work = fixture()
    publish(repository, journal, native, work)
    publication = only_publication(repository)
    receipt = journal.rows['publications', publication['publication_id']]
    receipt['result'].update(status='conflict', observed_description='*********(redacted)')
    original_receipt = deepcopy(receipt)
    expected = native.detail['description']
    native.detail['description'] = '*********(redacted)'
    result = publish(repository, journal, native, work)
    assert result['catalog_state'] == 'UNKNOWN' and not result['write_attempted']
    assert journal.rows['publications', publication['publication_id']] == original_receipt
    native.detail['description'] = expected
    assert publish(repository, journal, native, work)['catalog_state'] == 'VERIFIED'
    assert put_count(native) == 1
    assert journal.rows['publications', publication['publication_id']] == original_receipt
