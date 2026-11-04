from copy import deepcopy
from unittest.mock import Mock

import pytest

from test_ai_dg_publication_controller import fixture, publish, only_publication, put_count
from test_ai_dg_replication import prepare_replica, replicate


def masked_publication():
    repository, journal, native, work = fixture()
    publish(repository, journal, native, work)
    publication = only_publication(repository)
    receipt = journal.rows['publications', publication['publication_id']]
    receipt['result'].update(status='conflict', observed_description='*********(redacted)')
    native.detail['description'] = '*********(redacted)'
    return repository, journal, native, work


def test_first_write_confirms_masked_readback_only_after_the_claim_and_single_put():
    repository, journal, native, work = fixture()
    original = native.request
    def request(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if method == 'PUT':
            native.detail['description'] = '*********(redacted)'
        return result
    native.request = request
    def confirm(table, digest):
        publication = only_publication(repository)
        assert journal.read('claims', publication['publication_id']) is not None
        assert put_count(native) == 1
        return True
    assert publish(repository, journal, native, work, confirm_description=confirm)['catalog_state'] == 'VERIFIED'
    assert put_count(native) == 1


def test_recovery_confirms_only_the_existing_generated_header_hash_without_another_write():
    repository, journal, native, work = masked_publication()
    confirmation = Mock(return_value=True)
    before = deepcopy(journal.rows)
    result = publish(repository, journal, native, work, confirm_description=confirmation)
    assert result['catalog_state'] == 'VERIFIED' and not result['write_attempted']
    document, = repository.data['AI_DG_DOCUMENT'].values()
    confirmation.assert_called_once_with('catalog.schema.users', document['text_hash'])
    assert journal.rows == before and put_count(native) == 1
    assert native.detail['description'] == '*********(redacted)'
    prepare_replica(repository)
    result = replicate(repository, journal, native, work, confirm_description=confirmation)
    assert result['catalog_state'] == result['replica_state'] == 'VERIFIED'
    assert repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['description'] == document['description_text']
    assert put_count(native) == 1 and journal.rows == before


@pytest.mark.parametrize('confirmed', [False, None, 'true'])
def test_unconfirmed_mask_remains_unavailable(confirmed):
    repository, journal, native, work = masked_publication()
    confirmation = Mock(return_value=confirmed)
    assert publish(repository, journal, native, work, confirm_description=confirmation)['catalog_state'] == 'UNKNOWN'
    prepare_replica(repository)
    with pytest.raises(ValueError, match='redacted'):
        replicate(repository, journal, native, work, confirm_description=confirmation)
    assert put_count(native) == 1


def test_foreign_claim_cannot_authorize_hash_confirmation():
    repository, journal, native, work = masked_publication()
    publication = only_publication(repository)
    journal.rows['claims', publication['publication_id']]['document_id'] = 'foreign'
    confirmation = Mock(return_value=True)
    with pytest.raises(ValueError, match='claim'):
        publish(repository, journal, native, work, confirm_description=confirmation)
    confirmation.assert_not_called()
    assert put_count(native) == 1


def test_nonmasked_human_text_never_uses_confirmation():
    repository, journal, native, work = masked_publication()
    native.detail['description'] = 'Descripción humana'
    confirmation = Mock(return_value=True)
    assert publish(repository, journal, native, work, confirm_description=confirmation)['catalog_state'] == 'CONFLICT'
    confirmation.assert_not_called()
    assert native.detail['description'] == 'Descripción humana' and put_count(native) == 1


def test_confirmation_failure_keeps_the_receipt_pending_without_exposing_the_error():
    repository, journal, native, work = masked_publication()
    confirmation = Mock(side_effect=RuntimeError('private failure'))
    assert publish(repository, journal, native, work, confirm_description=confirmation)['catalog_state'] == 'UNKNOWN'
    assert put_count(native) == 1


def test_missing_claim_cannot_authorize_hash_confirmation():
    repository, journal, native, work = masked_publication()
    publication = only_publication(repository)
    del journal.rows['claims', publication['publication_id']]
    confirmation = Mock(return_value=True)
    assert publish(repository, journal, native, work, confirm_description=confirmation)['catalog_state'] == 'UNKNOWN'
    confirmation.assert_not_called()
    assert put_count(native) == 1


def test_column_mask_never_uses_header_confirmation():
    repository, journal, native, work = fixture(column=True)
    publish(repository, journal, native, work)
    publication = only_publication(repository)
    journal.rows['publications', publication['publication_id']]['result'].update(
        status='conflict', observed_description='*********(redacted)')
    native.detail['tableFields'][0]['fieldDescription'] = '*********(redacted)'
    confirmation = Mock(return_value=True)
    assert publish(repository, journal, native, work, confirm_description=confirmation)['catalog_state'] == 'UNKNOWN'
    confirmation.assert_not_called()
    assert put_count(native) == 1


def test_cancellation_after_hash_confirmation_prevents_replica_writes():
    repository, journal, native, work = masked_publication()
    prepare_replica(repository)
    before = deepcopy(repository.data)
    cancelled = False
    def confirm(*args):
        nonlocal cancelled
        cancelled = True
        return True
    def active():
        if cancelled:
            raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        replicate(repository, journal, native, work, confirm_description=confirm, check_active=active)
    assert repository.data == before and put_count(native) == 1


@pytest.mark.parametrize('value, matches', [('Known documentation [IA]', True), ('Different description', False),
    ('*********(redacted)', False), (None, False)])
def test_native_catalog_confirmation_reads_only_description_and_returns_boolean(monkeypatch, value, matches):
    import hashlib
    from types import ModuleType, SimpleNamespace
    import sys
    from app.ai_data_governance.native_pipeline import confirm_generated_header_hash
    utils = ModuleType('pyspark.sql.utils')
    utils.toJArray = lambda gateway, kind, values: values
    monkeypatch.setitem(sys.modules, 'pyspark', ModuleType('pyspark'))
    monkeypatch.setitem(sys.modules, 'pyspark.sql', ModuleType('pyspark.sql'))
    monkeypatch.setitem(sys.modules, 'pyspark.sql.utils', utils)
    properties = Mock()
    properties.get.return_value = value
    catalog = Mock()
    catalog.loadTable.return_value.properties.return_value = properties
    identifier = Mock()
    spark = Mock()
    spark._jsparkSession.sessionState.return_value.catalogManager.return_value.catalog.return_value = catalog
    spark._jvm = SimpleNamespace(java=SimpleNamespace(lang=SimpleNamespace(String=str)),
        org=SimpleNamespace(apache=SimpleNamespace(spark=SimpleNamespace(sql=SimpleNamespace(
            connector=SimpleNamespace(catalog=SimpleNamespace(Identifier=identifier)))))))
    digest = hashlib.sha256(b'Known documentation [IA]').hexdigest()
    assert confirm_generated_header_hash(spark, 'catalog.schema.users', digest) is matches
    properties.get.assert_called_once_with('description')
    spark._jsparkSession.sessionState.return_value.catalogManager.return_value.catalog.assert_called_once_with('catalog')
    identifier.of.assert_called_once_with(['schema'], 'users')


@pytest.mark.parametrize('table, digest', [('catalog.schema.users/escape', 'a' * 64),
    ('oci_medallion.oci_artifacts.ai_dg_asset', 'a' * 64), ('catalog.schema.users', 'not-a-hash')])
def test_invalid_native_hash_confirmation_never_accesses_spark(table, digest):
    from app.ai_data_governance.native_pipeline import confirm_generated_header_hash
    spark = Mock()
    assert confirm_generated_header_hash(spark, table, digest) is False
    assert not spark.mock_calls


def test_assembled_effect_runtime_includes_hash_confirmation_adapter(monkeypatch):
    import sys
    from types import ModuleType
    from app.ai_data_governance.pipeline_source import pipeline_task_source
    from test_ai_dg_workflow import binding
    module = ModuleType('hash_confirmation_runtime')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(pipeline_task_source(binding(), 'replicate_model'), 'replicate_model.py', 'exec'), module.__dict__)
    assert module.confirm_generated_header_hash(Mock(), 'invalid', 'a' * 64) is False
    context = {'table_key': 'catalog.schema.users', 'column_name': None, 'asset': {'asset_kind': 'TABLE'},
        'scope': {'catalog_key': 'catalog', 'schema_key': 'catalog.schema'},
        'document': {'description_text': 'Known [IA]', 'text_hash': 'a' * 64}}
    request = Mock(return_value=({'key': 'catalog.schema.users', 'description': '*********(redacted)'}, {}))
    wrapped = module.confirmed_generated_readback(request, context, Mock(return_value=True), Mock())
    assert wrapped('GET', '/tables/catalog.schema.users')[0]['description'] == 'Known [IA]'


def confirmed_replica_fixture():
    repository, journal, native, work = masked_publication()
    publish(repository, journal, native, work, confirm_description=Mock(return_value=True))
    prepare_replica(repository)
    replicate(repository, journal, native, work, confirm_description=Mock(return_value=True))
    return repository, journal, native, work


def test_next_inventory_reconfirms_known_versioned_header_without_generation():
    from app.ai_data_governance.catalog import CatalogReader, CatalogScope
    from app.ai_data_governance.native_pipeline import inventory_readback_reader
    repository, journal, native, work = confirmed_replica_fixture()
    scope = repository.data['AI_DG_SCOPE'][('scope',)]
    confirmation = Mock(return_value=True)
    reader = inventory_readback_reader(repository, CatalogReader(native.request), scope, confirmation, Mock())
    before = deepcopy(repository.data)
    observed = reader.read_table(CatalogScope(scope['catalog_key'], scope['schema_key']), 'catalog.schema.users')
    document, = repository.data['AI_DG_DOCUMENT'].values()
    assert observed['description'] == document['description_text']
    confirmation.assert_called_once_with('catalog.schema.users', document['text_hash'])
    assert repository.data == before and put_count(native) == 1


@pytest.mark.parametrize('kind', ['unknown', 'masked', 'pending', 'different_hash'])
def test_inventory_cannot_reconstruct_unknown_or_unconfirmed_header(kind):
    from app.ai_data_governance.catalog import CatalogReader, CatalogScope
    from app.ai_data_governance.native_pipeline import inventory_readback_reader
    repository, journal, native, work = confirmed_replica_fixture()
    scope = repository.data['AI_DG_SCOPE'][('scope',)]
    confirmation = Mock(return_value=kind != 'different_hash')
    if kind == 'unknown':
        repository.data['AI_DG_SCOPE_ASSET'].clear()
    elif kind == 'masked':
        repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['description'] = '*********(redacted)'
    elif kind == 'pending':
        repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['consistency_state'] = 'REPLICA_PENDING'
    reader = inventory_readback_reader(repository, CatalogReader(native.request), scope, confirmation, Mock())
    with pytest.raises(ValueError, match='redacted'):
        reader.read_table(CatalogScope(scope['catalog_key'], scope['schema_key']), 'catalog.schema.users')
    if kind != 'different_hash':
        confirmation.assert_not_called()
    assert put_count(native) == 1


def test_inventory_known_text_must_match_its_immutable_version():
    from app.ai_data_governance.catalog import CatalogReader, CatalogScope
    from app.ai_data_governance.native_pipeline import inventory_readback_reader
    repository, journal, native, work = confirmed_replica_fixture()
    scope = repository.data['AI_DG_SCOPE'][('scope',)]
    repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['description'] = 'Forged cache'
    confirmation = Mock(return_value=True)
    reader = inventory_readback_reader(repository, CatalogReader(native.request), scope, confirmation, Mock())
    with pytest.raises(ValueError, match='confirmed version'):
        reader.read_table(CatalogScope(scope['catalog_key'], scope['schema_key']), 'catalog.schema.users')
    confirmation.assert_not_called()


def test_terminal_masked_work_is_recovered_only_from_its_frozen_batch_once():
    from app.ai_data_governance.native_pipeline import recover_masked_batch_work
    from test_ai_dg_documentary_hooks import BATCH
    repository, journal, native, work = confirmed_replica_fixture()
    current = repository.data['AI_DG_WORK'][(work['work_id'],)]
    current['state'] = 'PRESERVED'
    repository.data['AI_DG_SCOPE_ASSET'][('scope', work['asset_id'])]['description'] = '*********(redacted)'
    row = {'scope_id': 'scope', 'batch_ref': BATCH}
    assert recover_masked_batch_work(repository, journal, row, [], 100, Mock()) == [current]
    assert recover_masked_batch_work(repository, journal, row, [current], 100, Mock()) == [current]
    assert current['state'] == 'PRESERVED' and put_count(native) == 1
    with pytest.raises(ValueError, match='budget'):
        recover_masked_batch_work(repository, journal, row, [], 0, Mock())
