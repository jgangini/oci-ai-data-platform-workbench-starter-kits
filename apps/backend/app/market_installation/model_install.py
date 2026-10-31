"""Prepare an empty-model bootstrap; this module never submits cloud operations."""
from __future__ import annotations

from app.notebooks import WORKSPACE_ROOT
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.schema import create_statements, definition, identifier, schema_sha256

MODULE_ROOT = WORKSPACE_ROOT + '/.control/modules/ai_market_intelligence'
NOTEBOOK_PATH = MODULE_ROOT + '/create_model.ipynb'
JOB_NAME = 'wf_ai_market_intelligence_model'


def model_notebook(catalog: str, framework_schema: str, *, storage_root: str = '') -> dict:
    model = definition()
    statements = create_statements(catalog, framework_schema, storage_root=storage_root)
    expected = []
    for table in model['tables']:
        parts = (catalog, framework_schema, table['name'])
        expected.append({**table, 'qualified': '.'.join(identifier(name) for name in parts),
                         'detail_name': '.'.join(parts),
                         'location': storage_root + '/' + table['name'] if storage_root else None})
    source = (
        'import json\n'
        + 'EXPECTED = ' + repr(expected) + '\n'
        + 'STATEMENTS = ' + repr(statements) + '\n'
        + 'MODEL_HASH = ' + repr(schema_sha256()) + '\n'
        + 'MODEL_VERSION = ' + repr(str(model['schema_version'])) + '\n'
        + '''def verify_table(table):
    name = table['qualified']
    properties = {row['key']: row['value'] for row in spark.sql('SHOW TBLPROPERTIES ' + name).collect()}
    if properties.get('ai_mi.module_id') != 'ai_market_intelligence' or properties.get('ai_mi.schema_sha256') != MODEL_HASH:
        raise RuntimeError('Existing table is not owned by this exact market model: ' + table['name'])
    if properties.get('ai_mi.schema_version') != MODEL_VERSION:
        raise RuntimeError('Native schema version mismatch: ' + table['name'])
    # The native Delta detail resolver expects an unquoted, validated name.
    detail = spark.sql('DESCRIBE DETAIL ' + table['detail_name']).first()
    if detail['format'] != 'delta' or detail['description'] != table['description']:
        raise RuntimeError('Native provider or table description mismatch: ' + table['name'])
    if table['location'] and str(detail['location']).rstrip('/') != table['location']:
        raise RuntimeError('Native artifacts location mismatch: ' + table['name'])
    actual = spark.table(name).schema
    if [field.name for field in actual] != [column['name'] for column in table['columns']]:
        raise RuntimeError('Native column order differs from the approved model: ' + table['name'])
    for field, column in zip(actual, table['columns']):
        if field.dataType.simpleString().upper() != column['type'] or field.nullable != column['nullable']:
            raise RuntimeError('Native type or nullability mismatch: ' + table['name'] + '.' + field.name)
        if field.metadata.get('comment') != column['description']:
            raise RuntimeError('Native column description mismatch: ' + table['name'] + '.' + field.name)
    if spark.table(name).limit(1).count():
        raise RuntimeError('Installation requires empty tables; existing data preserved: ' + table['name'])
    return {'table': table['name'], 'columns': len(actual), 'rows': 0, 'provider': detail['format']}

# Validate every existing table before creating any missing table.
for table in EXPECTED:
    if spark.catalog.tableExists(table['qualified']):
        verify_table(table)
for table, statement in zip(EXPECTED, STATEMENTS):
    if not spark.catalog.tableExists(table['qualified']):
        spark.sql(statement)
verified = [verify_table(table) for table in EXPECTED]
print('AI_MARKET_MODEL_READY:' + json.dumps({'schema_sha256': MODEL_HASH, 'tables': verified,
      'documented_columns': sum(item['columns'] for item in verified)}, sort_keys=True))
''')
    compile(source, 'create_model.py', 'exec')
    return {'nbformat': 4, 'nbformat_minor': 5,
            'metadata': {'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'},
                         'language_info': {'name': 'python'}},
            'cells': [{'cell_type': 'code', 'execution_count': None, 'metadata': {}, 'outputs': [],
                       'source': source.splitlines(keepends=True)}]}


def model_job(compute_key: str) -> dict:
    if not isinstance(compute_key, str) or not compute_key or any(ord(c) < 32 for c in compute_key):
        raise ValueError('An existing verified compute key is required')
    return {'name': JOB_NAME, 'path': MODULE_ROOT,
            'description': 'Create and verify the documented AI Market Intelligence Delta model',
            'maxConcurrentRuns': 1, 'continuous': None, 'schedule': None, 'queue': {'isEnabled': False},
            'jobClusters': [{'clusterKey': compute_key}],
            'tasks': [{'type': 'NOTEBOOK_TASK', 'taskKey': 'create_market_model', 'dependsOn': [],
                       'runIf': 'ALL_SUCCESS', 'maxRetries': 0, 'isRetryOnTimeout': False,
                       'notebookPath': NOTEBOOK_PATH, 'cluster': {'clusterKey': compute_key}, 'parameters': []}]}
