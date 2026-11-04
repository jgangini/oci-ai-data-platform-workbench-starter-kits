from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai_data_governance import repository
from test_ai_dg_repository import prepared, row


def test_publication_state_can_advance_without_changing_its_identity(monkeypatch):
    spark, target, _ = prepared(monkeypatch)
    before = row('AI_DG_PUBLICATION')
    after = {**before, 'catalog_state': 'VERIFIED', 'replica_state': 'PENDING'}
    spark.table.return_value.join.return_value.limit.return_value.collect.side_effect = [
        [SimpleNamespace(asDict=lambda **_: repository._comparable(before))],
        [SimpleNamespace(asDict=lambda **_: repository._comparable(after))]]
    assert target.update_current('AI_DG_PUBLICATION', [after], expected=[before]) == 1
    statement = spark.sql.call_args.args[0]
    assert 'WHEN MATCHED THEN UPDATE' in statement
    for field in ('publication_id', 'document_id', 'initiating_run_id', 'expected_native_version'):
        assert f't.`{field}` = s.' not in statement.split('WHEN MATCHED')[1]


@pytest.mark.parametrize('field', ['publication_id', 'document_id', 'initiating_run_id', 'expected_native_version'])
def test_publication_identity_is_not_changed_by_current_state_update(field):
    target = repository.DeltaRepository(Mock(), 'oci://artifacts@namespace/oci_artifacts')
    before = row('AI_DG_PUBLICATION')
    with pytest.raises(ValueError):
        target.update_current('AI_DG_PUBLICATION', [{**before, field: 'different'}], expected=[deepcopy(before)])
    target.spark.sql.assert_not_called()
