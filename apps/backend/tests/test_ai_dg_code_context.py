import json
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from app.ai_data_governance.code_context import WorkspaceCodeReader, source_references, workspace_code_path


PATH = '/Workspace/jobs/read.py'
KEY = '12345678-1234-1234-1234-123456789abc'
SOURCE = 'spark.read.table("cat.sch.users").select("ABC13")\nsecret="do-not-return"'


def test_literal_reference_is_not_execution_or_business_meaning():
    result = source_references(PATH, SOURCE)
    assert {(r['table_key'], r['column_name']) for r in result['references']} == {('cat.sch.users', None), ('cat.sch.users', 'ABC13')}
    assert all(r['line'] == 1 for r in result['references'])
    assert 'do-not-return' not in str(result)


@pytest.mark.parametrize('source', ['# spark.table("cat.sch.users")', 'x="spark.table()"', 'spark.table(name)', 'other.table("cat.sch.users")', 'spark.sql("select * from cat.sch.users")'])
def test_dynamic_sql_comments_and_unrelated_receivers_do_not_create_references(source):
    assert source_references(PATH, source)['references'] == []


@pytest.mark.parametrize('path', ['/Workspace/../x.py', '/Workspace//x.py', '/Workspace/a%2fb.py', '/Workspace/x.py?token=x', '/etc/x.py'])
def test_source_path_cannot_escape_workspace(path):
    with pytest.raises(ValueError):
        workspace_code_path(path)


def test_notebook_hash_excludes_outputs_and_markdown():
    notebook = {'metadata': {'language_info': {'name': 'python'}}, 'cells': [
        {'cell_type': 'markdown', 'source': SOURCE},
        {'cell_type': 'code', 'source': SOURCE.splitlines(True), 'outputs': ['private']} ]}
    first = source_references('/Workspace/n.ipynb', json.dumps(notebook))
    notebook['cells'][1]['outputs'] = ['changed']
    second = source_references('/Workspace/n.ipynb', json.dumps(notebook))
    assert first == second and all(r['cell'] == 2 for r in first['references'])


def test_unsupported_language_and_malformed_metadata_are_explicit():
    assert source_references('/Workspace/x.scala', SOURCE)['skipped_cells'] == 1
    with pytest.raises(ValueError):
        source_references('/Workspace/n.ipynb', json.dumps({'cells': [], 'metadata': {'language_info': []}}))
    with pytest.raises(ValueError):
        source_references(PATH, 'x' * (2 * 1024 * 1024 + 1))


@pytest.mark.parametrize('language', [['python'], {'name': 'python'}])
def test_untrusted_notebook_language_abstains_instead_of_crashing(language):
    notebook = json.dumps({'cells': [], 'metadata': {'language_info': {'name': language}}})
    with pytest.raises(ValueError):
        source_references('/Workspace/n.ipynb', notebook)


def test_reader_reuses_source_and_keeps_task_provenance_without_source_text():
    tasks = [{'type': 'PYTHON_TASK', 'source': 'WORKSPACE', 'filePath': PATH, 'taskKey': name} for name in ('one', 'two')]
    request = Mock(side_effect=[({'items': [{'key': KEY}]}, {}), ({'key': KEY, 'tasks': tasks}, {}), (SOURCE, {})])
    reader = WorkspaceCodeReader(request, 'workspace', check_active=Mock())
    refs = reader.find('cat.sch.users', 'ABC13')
    assert {r['task_key'] for r in refs} == {'one', 'two'}
    assert all(r['execution_evidence'] is False for r in refs)
    assert 'do-not-return' not in str(refs)
    assert reader.find('cat.sch.users') and request.call_count == 3
    assert request.call_args.args[1].endswith(quote(PATH, safe=''))


def test_reader_reports_unsupported_source_as_partial():
    request = Mock(side_effect=[({'items': [{'key': KEY}]}, {}), ({'key': KEY, 'tasks': [
        {'type': 'PYTHON_TASK', 'filePath': PATH, 'taskKey': 'one'}]}, {}), ('%sql select 1', {})])
    reader = WorkspaceCodeReader(request, 'workspace', check_active=Mock())
    assert reader.find('cat.sch.users') == [] and reader.truncated


def test_reader_rejects_changed_job_identity_without_source_read():
    request = Mock(side_effect=[({'items': [{'key': KEY}]}, {}), ({'key': 'different', 'tasks': []}, {})])
    reader = WorkspaceCodeReader(request, 'workspace', check_active=Mock())
    assert reader.find('cat.sch.users') == [] and reader.truncated and request.call_count == 2


def test_later_source_error_preserves_prior_references():
    tasks = [{'type': 'PYTHON_TASK', 'filePath': path, 'taskKey': name} for name, path in [('one', PATH), ('two', '/Workspace/missing.py')]]
    request = Mock(side_effect=[({'items': [{'key': KEY}]}, {}), ({'key': KEY, 'tasks': tasks}, {}), (SOURCE, {}), RuntimeError('denied')])
    reader = WorkspaceCodeReader(request, 'workspace', check_active=Mock())
    assert len(reader.find('cat.sch.users', 'ABC13')) == 1 and reader.truncated


def test_cancellation_is_not_converted_to_missing_source():
    request = Mock(return_value=({'items': [{'key': KEY}]}, {}))
    reader = WorkspaceCodeReader(request, 'workspace', check_active=Mock(side_effect=ValueError('canceled')))
    with pytest.raises(ValueError, match='canceled'):
        reader.find('cat.sch.users')


def test_service_transport_reads_text_only_for_canonical_workspace_source():
    from app.ai_data_governance.code_context import CodeContextTransport
    from test_ai_dg_lineage import transport
    base, session = transport()
    reader = CodeContextTransport.__new__(CodeContextTransport)
    reader.__dict__.update(base.__dict__)
    session.request.return_value.__enter__.return_value.iter_content.return_value = [SOURCE.encode()]
    path = f'/workspaces/{reader.workspace}/objects/{quote(PATH, safe="")}'
    assert reader('GET', path)[0] == SOURCE
    assert session.request.call_args.kwargs['headers']['Accept'] == '*/*'
    with pytest.raises(ValueError):
        reader('GET', path.replace('%2F', '%2f'))
    with pytest.raises(ValueError):
        reader('POST', f'/workspaces/{reader.workspace}/jobs', payload={})
    with pytest.raises(ValueError):
        reader('GET', f'/workspaces/{reader.workspace}/jobs', params={'limit': '1000'})


def test_static_reference_is_separate_partial_evidence_when_lineage_is_empty(monkeypatch):
    from types import SimpleNamespace
    from app.ai_data_governance import native_pipeline
    from test_ai_dg_workflow_recovery import staging_fixture, observations, binding
    repository, _, _, _ = staging_fixture()
    records = observations()
    reader = Mock()
    reader.read.return_value = SimpleNamespace(nodes=(), links=(), pagination_complete=True, omitted_nodes=0, unresolved_links=0)
    monkeypatch.setattr(native_pipeline, 'LineageReader', Mock(return_value=reader))
    cfg = {**binding(), 'source_scopes': [{'catalog_key': 'catalog', 'schema_key': 'catalog.schema'}]}
    code = SimpleNamespace(find=Mock(return_value=[{'source_path': PATH, 'execution_evidence': False}]), truncated=False)
    context, evidence = native_pipeline.collect_usage_context(repository, Mock(), cfg, records.assets[0], records.versions[0], 'scope', 'run', code_reader=code)
    assert json.loads(context)['static_analysis_complete'] is False
    assert len(evidence) == 1 and evidence[0]['evidence_kind'] == 'STATIC_CODE'
    assert evidence[0]['coverage_state'] == 'PARTIAL'


DDL="CREATE TABLE IF NOT EXISTS `cat`.`sch`.`users` (`id` INT) USING DELTA COMMENT 'PRIVATE-DO-NOT-RETURN'"


@pytest.mark.parametrize("source",[
    f"spark.sql({DDL!r})",
    f"STATEMENTS=[{DDL!r}]\nfor statement in STATEMENTS:\n    spark.sql(statement)",
    f"STATEMENTS=({DDL!r},)\nEXPECTED=['users']\nfor table,statement in zip(EXPECTED,STATEMENTS):\n    if True:\n        spark.sql(statement)",
])
def test_literal_create_used_by_spark_is_static_table_evidence_only(source):
    result=source_references(PATH,source)
    assert len(result['references'])==1
    ref=result['references'][0]
    assert ref['table_key']=='cat.sch.users' and ref['column_name'] is None
    assert ref['operation']=='literal_sql_create_table_reference'
    assert 'PRIVATE-DO-NOT-RETURN' not in str(result) and 'USING DELTA' not in str(result)


@pytest.mark.parametrize("source",[
    f"STATEMENTS=[{DDL!r}]",
    f"other.sql({DDL!r})",
    f"STATEMENTS=[{DDL!r}]\nSTATEMENTS=[]\nfor s in STATEMENTS:\n    spark.sql(s)",
    f"STATEMENTS=[{DDL!r}]\nSTATEMENTS.append('other')\nfor s in STATEMENTS:\n    spark.sql(s)",
    f"STATEMENTS=[{DDL!r}]\nalias=STATEMENTS\nfor s in STATEMENTS:\n    spark.sql(s)",
    f"STATEMENTS=[{DDL!r}]\nfor s in STATEMENTS:\n    s='unknown'\n    spark.sql(s)",
    f"STATEMENTS=[{DDL!r}]\nEXPECTED=['name']\nfor table,s in zip(EXPECTED,STATEMENTS):\n    spark.sql(table)",
    "spark.sql('CREATE TABLE cat.sch.users.extra (id INT)')",
    "spark.sql('CREATE TABLE sch.users (id INT)')",
    "spark.sql(f'CREATE TABLE {table} (id INT)')",
    f"# spark.sql({DDL!r})",
])
def test_ddl_extraction_does_not_guess_unused_dynamic_or_mutated_statements(source):
    assert source_references(PATH,source)['references']==[]


def test_notebook_ddl_reference_keeps_cell_and_call_line_without_source():
    source=f"STATEMENTS=[{DDL!r}]\nfor s in STATEMENTS:\n    spark.sql(s)"
    notebook={'metadata':{'language_info':{'name':'python'}},'cells':[{'cell_type':'code','source':source,'outputs':['PRIVATE']} ]}
    result=source_references('/Workspace/n.ipynb',json.dumps(notebook))
    assert result['references'][0]['line']==3 and result['references'][0]['cell']==1
    assert 'PRIVATE' not in str(result)


@pytest.mark.parametrize("suffix", [
    "STATEMENTS: list[str] = supplied\nfor s in STATEMENTS:\n    spark.sql(s)",
    "def consume(STATEMENTS):\n    for s in STATEMENTS:\n        spark.sql(s)",
    "for STATEMENTS in supplied:\n    for s in STATEMENTS:\n        spark.sql(s)",
    "EXPECTED=[]\nfor _,s in zip(EXPECTED,STATEMENTS):\n    spark.sql(s)",
    "for _,s in zip(unknown,STATEMENTS):\n    spark.sql(s)",
])
def test_ddl_binding_abstains_on_shadowing_and_unresolved_zip(suffix):
    assert source_references(PATH, f"STATEMENTS=[{DDL!r}]\n"+suffix)['references']==[]


def test_ddl_zip_references_only_reachable_literal_positions():
    other="CREATE TABLE cat.sch.unreachable (id INT)"
    source=f"STATEMENTS=[{DDL!r},{other!r}]\nEXPECTED=[{{'name':'users'}}]\nfor _,s in zip(EXPECTED,STATEMENTS):\n    spark.sql(s)"
    refs=source_references(PATH,source)['references']
    assert [r['table_key'] for r in refs]==['cat.sch.users']


def test_ddl_loop_binding_is_not_inherited_by_a_shadowing_nested_parameter():
    source=f"STATEMENTS=[{DDL!r}]\nfor s in STATEMENTS:\n    def consume(s):\n        spark.sql(s)"
    assert source_references(PATH,source)['references']==[]
