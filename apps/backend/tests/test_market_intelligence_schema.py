"""Model documentation and safe native DDL, without a cloud connection."""
import copy

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import schema


def test_installer_storage_is_dedicated_to_the_selected_artifacts_bucket():
    root = 'oci://selected-artifacts@namespace/ai_market_intelligence/delta'
    statements = schema.create_statements('oci_medallion', 'oci_artifacts', storage_root=root)
    assert len(statements) == 14
    for table, statement in zip(schema.definition()['tables'], statements):
        assert "LOCATION '" + root + '/' + table['name'] + "'" in statement
        assert '`oci_medallion`.`oci_artifacts`.' in statement
    for invalid in ('oci://bucket@ns/shared', root + "';DROP TABLE x", 'https://example.test/'):
        with pytest.raises(ValueError, match='dedicated artifacts'):
            schema.create_statements('oci_medallion', 'oci_artifacts', storage_root=invalid)


def test_approved_tables_belong_to_the_agent_framework_and_keep_all_column_comments():
    model = schema.definition()
    assert len(model['tables']) == 14
    assert sum(len(table['columns']) for table in model['tables']) == 145
    assert model['schema_version'] == 2
    assert {table['layer'] for table in model['tables']} == {'framework'}
    for table in model['tables']:
        scoped = {column['name']: column for column in table['columns']}
        assert not scoped['tenant_id']['nullable'] and not scoped['owner_id']['nullable']
        assert all(column['description'].strip() for column in table['columns'])


def test_native_ddl_preserves_comments_without_silent_reuse():
    statements = schema.create_statements('oci_medallion', 'oci_artifacts')
    assert len(statements) == 14
    assert all('IF NOT EXISTS' not in statement and 'USING DELTA' in statement for statement in statements)
    assert sum(statement.count(' COMMENT ') for statement in statements) == 145
    assert all('ai_mi.schema_sha256' in statement for statement in statements)
    assert all('CREATE TABLE `oci_medallion`.`oci_artifacts`.' in statement for statement in statements)
    assert '`oci_gold`' not in '\n'.join(statements)
    assert '`oci_medallion`.`oci_artifacts`.`ai_mi_reports`' in '\n'.join(statements)
    assert '`oci_medallion`.`oci_artifacts`.`ai_mi_schedules`' in '\n'.join(statements)


@pytest.mark.parametrize('bad', ['oci_gold; DROP SCHEMA x', 'a.b', '', 'name`', '../other'])
def test_schema_identifiers_cannot_escape_the_qualified_name(bad):
    with pytest.raises(ValueError):
        schema.create_statements('oci_medallion', bad)


def test_dictionary_and_definition_share_every_column():
    text = schema.data_dictionary()
    assert text.count('| Sí |') + text.count('| No |') == 145
    assert 'microusd' in text and 'nulo' in text


def test_missing_column_documentation_is_rejected(monkeypatch, tmp_path):
    import json
    model = copy.deepcopy(schema.definition())
    model['tables'][0]['columns'][0]['description'] = ''
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps(model), encoding='utf-8')
    monkeypatch.setattr(schema, 'SCHEMA_PATH', path)
    with pytest.raises(ValueError, match='description'):
        schema.definition()


def test_native_model_job_has_no_schedule_or_automatic_retries():
    from app.market_installation.model_install import model_job
    job = model_job('verified-compute')
    assert job['schedule'] is None and job['continuous'] is None
    assert job['maxConcurrentRuns'] == 1 and not job['queue']['isEnabled']
    assert len(job['tasks']) == 1
    assert job['tasks'][0]['maxRetries'] == 0 and not job['tasks'][0]['isRetryOnTimeout']


def test_native_notebook_checks_existing_ownership_and_all_column_comments():
    from app.market_installation.model_install import model_notebook
    notebook = model_notebook('oci_medallion', 'oci_artifacts')
    source = ''.join(notebook['cells'][0]['source'])
    compile(source, 'native_model.py', 'exec')
    assert "properties.get('ai_mi.schema_sha256') != MODEL_HASH" in source
    assert "field.metadata.get('comment') != column['description']" in source
    assert 'DROP TABLE' not in source and 'INSERT ' not in source


def test_dictionary_contains_actual_reference_targets():
    text = schema.data_dictionary()
    for table in schema.definition()['tables']:
        for column in table['columns']:
            if column.get('references'):
                assert column['references'] in text


@pytest.mark.parametrize('fault', ['foreign_owner', 'changed_comment', 'changed_type', 'changed_nullability',
                                 'changed_version', 'changed_provider', 'changed_table_comment', 'existing_rows'])
def test_native_resume_rejects_existing_table_drift(fault):
    from app.market_installation.model_install import model_notebook

    spark = ModelSpark(fault=fault, missing_first=True)
    original = copy.deepcopy(spark.rows)
    source = ''.join(model_notebook('oci_medallion', 'oci_artifacts')['cells'][0]['source'])
    with pytest.raises(RuntimeError, match='Existing table|mismatch|existing data preserved'):
        exec(compile(source, 'native_model.py', 'exec'), {'spark': spark})
    assert not spark.created
    assert spark.rows == original


class ModelSpark:
    """Minimal Spark surface to execute the generated installation notebook."""

    def __init__(self, fault=None, missing_first=False, fresh=False):
        from types import SimpleNamespace
        self.tables = {'.'.join(schema.identifier(part) for part in (
            'oci_medallion', 'oci_artifacts', table['name'])):
            table for table in schema.definition()['tables']}
        self.names = list(self.tables)
        self.present = set() if fresh else set(self.names[1:] if missing_first else self.names)
        self.fault_name = self.names[-1]
        self.fault = fault
        self.created = []
        self.rows = {name: ([{'retained': 'business-data'}] if
            fault == 'existing_rows' and name == self.fault_name else []) for name in self.names}
        self.catalog = SimpleNamespace(tableExists=lambda name: name in self.present)

    def sql(self, statement):
        from types import SimpleNamespace
        if statement.startswith('CREATE TABLE '):
            name = statement[len('CREATE TABLE '):].split(' (', 1)[0]
            assert name in self.tables and name not in self.present
            self.present.add(name)
            self.created.append(name)
            return None
        if statement.startswith('SHOW TBLPROPERTIES '):
            name = statement[len('SHOW TBLPROPERTIES '):]
            properties = {'ai_mi.module_id': 'ai_market_intelligence', 'ai_mi.schema_version': '2',
                          'ai_mi.schema_sha256': schema.schema_sha256()}
            if name == self.fault_name and self.fault == 'foreign_owner':
                properties['ai_mi.module_id'] = 'another_module'
            if name == self.fault_name and self.fault == 'changed_version':
                properties['ai_mi.schema_version'] = '1'
            return SimpleNamespace(collect=lambda: [{'key': k, 'value': v} for k, v in properties.items()])
        assert statement.startswith('DESCRIBE DETAIL ')
        plain = statement[len('DESCRIBE DETAIL '):]
        if '`' in plain:
            raise RuntimeError('AIDP Delta resolver cannot resolve quoted namespace characters')
        name = '.'.join('`' + part + '`' for part in plain.split('.'))
        detail = {'format': 'delta', 'description': self.tables[name]['description']}
        if name == self.fault_name and self.fault == 'changed_provider':
            detail['format'] = 'parquet'
        if name == self.fault_name and self.fault == 'changed_table_comment':
            detail['description'] = 'old description'
        return SimpleNamespace(first=lambda: detail)

    def table(self, name):
        from types import SimpleNamespace
        assert name in self.present
        fields = [SimpleNamespace(name=column['name'], nullable=column['nullable'],
            dataType=SimpleNamespace(simpleString=lambda value=column['type']: value.lower()),
            metadata={'comment': column['description']}) for column in self.tables[name]['columns']]
        if name == self.fault_name:
            if self.fault == 'changed_comment':
                fields[0].metadata['comment'] = 'outdated'
            elif self.fault == 'changed_type':
                fields[0].dataType = SimpleNamespace(simpleString=lambda: 'bigint')
            elif self.fault == 'changed_nullability':
                fields[0].nullable = not fields[0].nullable
        return SimpleNamespace(schema=fields, limit=lambda limit:
            SimpleNamespace(count=lambda: min(limit, len(self.rows[name]))))


@pytest.mark.parametrize('fresh', [True, False])
def test_native_empty_install_and_resume_verify_complete_model(fresh, capsys):
    import json
    from app.market_installation.model_install import model_notebook
    spark = ModelSpark(fresh=fresh)
    source = ''.join(model_notebook('oci_medallion', 'oci_artifacts')['cells'][0]['source'])
    exec(compile(source, 'native_model.py', 'exec'), {'spark': spark})
    receipt = json.loads(capsys.readouterr().out.split('AI_MARKET_MODEL_READY:')[1])
    assert len(spark.created) == (14 if fresh else 0)
    assert receipt['documented_columns'] == 145
    assert len(receipt['tables']) == 14
    assert all(table['provider'] == 'delta' and table['rows'] == 0 for table in receipt['tables'])


def test_python_bootstrap_initializes_spark_and_executes_the_same_empty_model(monkeypatch, capsys):
    import json
    import sys
    from types import SimpleNamespace
    from app.market_installation.model_install import model_script
    spark = ModelSpark(fresh=True)
    monkeypatch.setitem(sys.modules, 'pyspark.sql', SimpleNamespace(
        SparkSession=SimpleNamespace(builder=SimpleNamespace(getOrCreate=lambda: spark))))
    exec(compile(model_script('oci_medallion', 'oci_artifacts'), 'model.py', 'exec'), {})
    receipt = json.loads(capsys.readouterr().out.split('AI_MARKET_MODEL_READY:')[1])
    assert len(spark.created) == 14 and receipt['documented_columns'] == 145
    assert all(table['rows'] == 0 for table in receipt['tables'])


def test_native_partial_model_creation_resumes_without_replacing_tables():
    from app.market_installation.model_install import model_notebook
    spark = ModelSpark(missing_first=True)
    source = ''.join(model_notebook('oci_medallion', 'oci_artifacts')['cells'][0]['source'])
    exec(compile(source, 'native_model.py', 'exec'), {'spark': spark})
    assert spark.created == [spark.names[0]]


@pytest.mark.parametrize('catalog,framework', [('bad;catalog', 'oci_artifacts'), ('oci_medallion', 'bad.schema')])
def test_model_notebook_rejects_unvalidated_delta_detail_names(catalog, framework):
    from app.market_installation.model_install import model_notebook
    with pytest.raises(ValueError, match='plain SQL identifier'):
        model_notebook(catalog, framework)
