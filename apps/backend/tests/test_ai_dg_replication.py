from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import Mock

import pytest

from app.ai_data_governance.catalog import CatalogReader
from app.ai_data_governance.replication import replicate_work
from app.ai_data_governance.inventory_writer import persist_inventory
from test_ai_dg_inventory_writer import AT, observations
from test_ai_dg_publication_controller import fixture, publish, only_publication, put_count


def prepare_replica(repository):
    writer = deepcopy(repository.data['AI_DG_RUN'][('run',)])
    manifest = json.loads(writer['manifest_json'])
    writer.update(run_id='replica', execution_key='replica-execution', native_run_ref='native:replica',
        manifest_json=json.dumps({**manifest, 'task_key': 'replicate_model'}))
    repository.insert_immutable('AI_DG_RUN', [writer])


def replicate(repository, journal, native, work, **kwargs):
    return replicate_work(repository, journal, CatalogReader(native.request), 'scope', work['work_id'], 'replica',
        check_active=kwargs.pop('check_active', Mock()), observed_at=kwargs.pop('observed_at', AT + timedelta(minutes=1)),
        **kwargs)


@pytest.mark.parametrize('column', [False, True])
def test_catalog_readback_is_replicated_to_versions_and_projection_before_completion(column):
    repository, journal, native, work = fixture(column)
    publish(repository, journal, native, work)
    prepare_replica(repository)
    expected = next(iter(repository.data['AI_DG_DOCUMENT'].values()))['description_text']
    result = replicate(repository, journal, native, work)
    assert result['catalog_state'] == result['replica_state'] == 'VERIFIED'
    assert result['catalog_writes'] == result['model_calls'] == 0
    publication = only_publication(repository)
    version = repository.data['AI_DG_ASSET_VERSION'][(publication['replica_version_id'],)]
    projection = repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]
    assert version['description'] == projection['description'] == expected
    assert version['description_origin'] == projection['description_origin'] == 'EXTERNAL_UNVERIFIED'
    assert projection['consistency_state'] == 'VERIFIED'
    assert repository.data['AI_DG_WORK'][(work['work_id'],)]['state'] == 'DONE'
    before = deepcopy(repository.data)
    replicate(repository, journal, native, work, observed_at=AT + timedelta(hours=1))
    assert repository.data == before and put_count(native) == 1


def test_replica_failure_does_not_republish_or_regenerate_and_retains_original_snapshot():
    repository, journal, native, work = fixture()
    publish(repository, journal, native, work)
    prepare_replica(repository)
    repository.fail_table = 'AI_DG_ASSET_VERSION'
    with pytest.raises(TimeoutError):
        replicate(repository, journal, native, work)
    assert only_publication(repository)['replica_state'] == 'PENDING'
    assert repository.data['AI_DG_WORK'][(work['work_id'],)]['state'] != 'DONE'
    versions = deepcopy(repository.data['AI_DG_ASSET_VERSION'])
    replicate(repository, journal, native, work, observed_at=AT + timedelta(minutes=2))
    for key, row in versions.items():
        assert repository.data['AI_DG_ASSET_VERSION'][key] == row
    assert only_publication(repository)['replica_state'] == 'VERIFIED' and put_count(native) == 1
    assert len(repository.data['AI_DG_DOCUMENT']) == 1


def test_changed_external_description_is_copied_and_preserved_instead_of_restoring_ai_text():
    repository, journal, native, work = fixture()
    publish(repository, journal, native, work)
    native.detail['description'] = 'Edición humana posterior'
    prepare_replica(repository)
    result = replicate(repository, journal, native, work)
    assert result['catalog_state'] == 'CONFLICT' and result['replica_state'] == 'VERIFIED'
    assert repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['description'] == 'Edición humana posterior'
    assert repository.data['AI_DG_WORK'][(work['work_id'],)]['state'] == 'PRESERVED'
    assert put_count(native) == 1


def test_unknown_write_with_still_blank_source_stays_pending_without_another_put():
    repository, journal, native, work = fixture()
    request = native.request
    def delayed(method, path, **kwargs):
        if method == 'PUT':
            native.calls.append((method, path, kwargs))
            raise TimeoutError('still executing remotely')
        return request(method, path, **kwargs)
    native.request = delayed
    publish(repository, journal, native, work)
    prepare_replica(repository)
    result = replicate(repository, journal, native, work)
    assert result['catalog_state'] == 'UNKNOWN' and result['replica_state'] == 'PENDING'
    assert repository.data['AI_DG_WORK'][(work['work_id'],)]['state'] == 'RETRY'
    assert repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['consistency_state'] == 'UPDATE_PENDING'
    assert put_count(native) == 1


def test_missing_native_description_cannot_replace_the_confirmed_projection_with_null():
    repository, journal, native, work = fixture()
    publish(repository, journal, native, work)
    prepare_replica(repository)
    native.detail.pop('description')
    before = deepcopy(repository.data)
    with pytest.raises(ValueError, match='description coverage'):
        replicate(repository, journal, native, work)
    assert repository.data == before and put_count(native) == 1


def test_replica_cancellation_before_read_or_commit_does_not_confirm_success():
    repository, journal, native, work = fixture()
    publish(repository, journal, native, work)
    prepare_replica(repository)
    before = deepcopy(repository.data)
    with pytest.raises(RuntimeError, match='cancelled'):
        replicate(repository, journal, native, work, check_active=Mock(side_effect=RuntimeError('cancelled')))
    assert repository.data == before and put_count(native) == 1


def test_unstarted_intent_remains_publishable_after_all_done_replication():
    repository, journal, native, work = fixture()
    read = journal.read
    def unavailable(kind, key):
        if kind == 'publications':
            raise RuntimeError('temporary journal read failure')
        return read(kind, key)
    journal.read = unavailable
    with pytest.raises(RuntimeError, match='journal read'):
        publish(repository, journal, native, work)
    assert only_publication(repository)['catalog_state'] == 'PREPARED' and put_count(native) == 0
    journal.read = read
    prepare_replica(repository)
    assert replicate(repository, journal, native, work)['catalog_state'] == 'PREPARED'
    assert repository.data['AI_DG_WORK'][(work['work_id'],)]['phase'] == 'PUBLISH'
    assert publish(repository, journal, native, work)['catalog_state'] == 'VERIFIED'
    assert put_count(native) == 1


def test_lost_work_completion_after_copying_external_text_without_a_start_claim_can_reconcile():
    repository, journal, native, work = fixture()
    read = journal.read
    journal.read = Mock(side_effect=RuntimeError('journal unavailable before start'))
    with pytest.raises(RuntimeError):
        publish(repository, journal, native, work)
    journal.read = read
    native.detail['description'] = 'Descripción humana llegada antes de escribir'
    prepare_replica(repository)
    update = repository.update_current
    def lost(table, rows, *, expected):
        if table == 'AI_DG_WORK' and rows:
            raise TimeoutError('completion was not committed')
        return update(table, rows, expected=expected)
    repository.update_current = lost
    with pytest.raises(TimeoutError):
        replicate(repository, journal, native, work)
    assert only_publication(repository)['replica_state'] == 'VERIFIED'
    assert not journal.read('claims', only_publication(repository)['publication_id'])
    repository.update_current = update
    result = replicate(repository, journal, native, work, observed_at=AT + timedelta(minutes=2))
    assert result['catalog_state'] == 'CONFLICT' and result['replica_state'] == 'VERIFIED'
    assert repository.data['AI_DG_WORK'][(work['work_id'],)]['state'] == 'PRESERVED' and put_count(native) == 0


@pytest.mark.parametrize('clear_before_replica', [False, True])
def test_reappearing_original_blank_revision_reopens_work_without_hiding_an_external_clear(clear_before_replica):
    repository, journal, native, work = fixture()
    publish(repository, journal, native, work)
    prepare_replica(repository)
    if clear_before_replica:
        native.detail['description'] = None
    result = replicate(repository, journal, native, work)
    assert result['replica_state'] == 'VERIFIED'
    if clear_before_replica:
        assert result['catalog_state'] == 'CONFLICT'
        assert only_publication(repository)['last_error_code'] == 'DESCRIPTION_CLEARED_EXTERNALLY'
    old = deepcopy(repository.data['AI_DG_WORK'][(work['work_id'],)])
    incoming = observations(hour=1)
    assert old['expected_version_id'] == incoming.work[0]['expected_version_id']
    persist_inventory(repository, 'scope', 'run', incoming)
    reopened = repository.data['AI_DG_WORK'][(work['work_id'],)]
    assert reopened['phase'] == 'DOCUMENT' and reopened['selected_document_id'] is None
    assert reopened['state'] == 'WAIT_CONTEXT'
    assert len(repository.data['AI_DG_DOCUMENT']) == 1 and put_count(native) == 1


def test_redacted_readback_cannot_be_replicated_as_verified_metadata():
    repository, journal, native, work = fixture()
    publish(repository, journal, native, work)
    prepare_replica(repository)
    native.detail['description'] = '*********(redacted)'
    before = deepcopy(repository.data)
    with pytest.raises(ValueError, match='redacted'):
        replicate(repository, journal, native, work)
    assert repository.data == before
    assert only_publication(repository)['replica_state'] == 'PENDING'
    assert put_count(native) == 1


def test_historically_verified_masked_projection_requires_fresh_replica_read():
    repository, journal, native, work = fixture()
    publish(repository, journal, native, work)
    prepare_replica(repository)
    replicate(repository, journal, native, work)
    expected = native.detail['description']
    repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['description'] = '*********(redacted)'
    native.detail['description'] = '*********(redacted)'
    with pytest.raises(ValueError, match='redacted'):
        replicate(repository, journal, native, work)
    native.detail['description'] = expected
    assert replicate(repository, journal, native, work)['replica_state'] == 'VERIFIED'
    assert repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['description'] == expected
    assert put_count(native) == 1


def test_old_masked_conflict_does_not_override_a_fresh_matching_replica():
    repository, journal, native, work = fixture()
    publish(repository, journal, native, work)
    publication = only_publication(repository)
    publication.update(catalog_state='CONFLICT', replica_state='VERIFIED')
    receipt = journal.rows['publications', publication['publication_id']]
    receipt['result'].update(status='conflict', observed_description='*********(redacted)')
    original_receipt = deepcopy(receipt)
    repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['description'] = '*********(redacted)'
    repository.data['AI_DG_WORK'][(work['work_id'],)].update(phase='REPLICATE', state='PRESERVED')
    prepare_replica(repository)
    result = replicate(repository, journal, native, work)
    assert result['catalog_state'] == result['replica_state'] == 'VERIFIED'
    assert repository.data['AI_DG_WORK'][(work['work_id'],)]['state'] == 'DONE'
    assert journal.rows['publications', publication['publication_id']] == original_receipt
    assert put_count(native) == 1

def test_historically_verified_masked_projection_cannot_short_circuit_publication():
    repository, journal, native, work = fixture()
    publish(repository, journal, native, work)
    prepare_replica(repository)
    replicate(repository, journal, native, work)
    repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['description'] = '*********(redacted)'
    native.detail['description'] = '*********(redacted)'
    result = publish(repository, journal, native, work)
    assert result['catalog_state'] == 'UNKNOWN' and result['replica_state'] == 'PENDING'
    assert repository.data['AI_DG_WORK'][(work['work_id'],)]['state'] == 'RETRY'
    assert put_count(native) == 1
