from unittest.mock import Mock

import pytest

from app.modules.ai_data_governance.consultation import ConsultationHooks
from app.modules.ai_data_governance.inventory import identity
from app.modules.ai_data_governance.lineage import LineageTaskReader


JOB = '22222222-2222-4222-8222-222222222222'
WORKSPACE = '11111111-1111-4111-8111-111111111111'
TASK = 'synthetic_lineage_transform'
PLATFORM = 'platform'
URI = f'aidp://workflows@{PLATFORM}/{WORKSPACE}/o/{JOB}/{TASK}'


def task_node(**changes):
    return dict(id='task', type='Task', qualifiedName=URI, displayName=TASK, **changes)


def job():
    return dict(key=JOB, workspaceKey=WORKSPACE, tasks=[dict(taskKey=TASK)])


def test_native_task_admission_uses_exact_workspace_job_and_caches_the_read():
    request = Mock(return_value=(job(), {}))
    reader = LineageTaskReader(request, PLATFORM, WORKSPACE)
    assert reader.visible(task_node()) and reader.visible(task_node())
    request.assert_called_once_with('GET', f'/workspaces/{WORKSPACE}/jobs/{JOB}')


@pytest.mark.parametrize('change', [
    {'qualifiedName': URI.replace('@platform/', '@other/')},
    {'qualifiedName': URI.replace(WORKSPACE, 'other-workspace')},
    {'qualifiedName': URI + '/extra'}, {'qualifiedName': URI.replace(TASK, '../escape')},
    {'qualifiedName': URI.replace(TASK, 'encoded%2Ftask')},
    {'qualifiedName': URI.replace(JOB, 'not-a-uuid')}, {'type': 'Table'},
])
def test_untrusted_or_foreign_task_locator_is_rejected_before_io(change):
    request = Mock()
    node = task_node()
    node.update(change)
    assert not LineageTaskReader(request, PLATFORM, WORKSPACE).visible(node)
    request.assert_not_called()


@pytest.mark.parametrize('response', [dict(key='other', tasks=[dict(taskKey=TASK)]),
    dict(key=JOB, workspaceKey='other', tasks=[dict(taskKey=TASK)]),
    dict(key=JOB, tasks=[]), dict(key=JOB, tasks=[dict(taskKey=TASK)] * 2),
    dict(key=JOB, tasks=[dict(taskKey=TASK)] * 65), dict(key=JOB, tasks=[None]), None])
def test_unverified_current_job_or_ambiguous_task_never_grants_visibility(response):
    reader = LineageTaskReader(Mock(return_value=(response, {})), PLATFORM, WORKSPACE)
    assert not reader.visible(task_node())


def test_permission_failure_is_cached_and_does_not_reveal_the_task():
    request = Mock(side_effect=PermissionError('denied'))
    reader = LineageTaskReader(request, PLATFORM, WORKSPACE)
    assert not reader.visible(task_node()) and not reader.visible(task_node())
    assert request.call_count == 1


def test_task_lookup_has_a_fixed_budget_for_distinct_native_jobs():
    request = Mock(return_value=({}, {}))
    reader = LineageTaskReader(request, PLATFORM, WORKSPACE)
    for number in range(24):
        node = task_node()
        node['qualifiedName'] = URI.replace(JOB, f'00000000-0000-0000-0000-{number:012x}')
        assert not reader.visible(node)
    assert request.call_count == 16


@pytest.mark.parametrize('permission', [True, False])
def test_consultation_preserves_native_source_task_destination_direction_or_explicit_omission(permission):
    source, derived = 'catalog.schema.source', 'catalog.schema.derived'
    binding = dict(platform_id=PLATFORM, workspace_key=WORKSPACE,
        source_scopes=[dict(catalog_key='catalog', schema_key='catalog.schema')])
    scope_id = identity('AI_DG_SCOPE', PLATFORM, WORKSPACE, 'catalog', 'catalog.schema')
    nodes = [dict(id='source', type='Table', qualifiedName=f'aidp://catalogs@{PLATFORM}/o/{source}'),
        task_node(), dict(id='derived', type='Table', qualifiedName=f'aidp://catalogs@{PLATFORM}/o/{derived}')]
    links = [dict(fromNodeId='source', toNodeId='task'), dict(fromNodeId='task', toNodeId='derived')]

    def request(method, path, **kwargs):
        if path == '/actions/fetchLineage':
            return dict(nodes=nodes, links=links), {}
        if path.endswith('/jobs/' + JOB):
            if not permission:
                raise PermissionError('not readable')
            return job(), {}
        key = path.removeprefix('/tables/')
        assert method == 'GET' and key in (source, derived)
        return dict(key=key, description='Synthetic test table', tableFields=[]), {}

    caller = Mock(side_effect=request)
    caller.check_identity = Mock()
    hooks = ConsultationHooks(binding, caller, Mock())
    row = dict(scope_id=scope_id, table_key=derived, column_name=None, qualified_name=derived,
        asset_id='asset', version_id='version')
    proof = hooks._lineage(row)
    facts = proof['facts']
    assert facts['execution_evidence'] is False and facts['absence_proves_global_isolation'] is False
    assert facts['traversal_depth'] == 2
    if permission:
        assert facts['links'] == links and len(facts['nodes']) == 3
        assert proof['coverage'] == 'complete'
    else:
        assert facts['links'] == [] and facts['omitted_nodes'] == 1 and facts['unresolved_links'] == 2
        assert proof['coverage'] == 'partial'
        assert TASK not in str(proof)


@pytest.mark.parametrize('runtime', ['governance', 'sync_inventory'])
def test_generated_runtime_actually_admits_task_without_missing_imports(runtime, monkeypatch):
    import sys
    from types import ModuleType
    from app.modules.ai_data_governance.governance_source import governance_agent_source
    from app.modules.ai_data_governance.pipeline_source import pipeline_task_source
    from tests.support.ai_dg_fixtures import binding as governance_binding
    from tests.modules.ai_data_governance.test_ai_dg_workflow import binding as pipeline_binding

    if runtime == 'governance':
        source = governance_agent_source(governance_binding())
    else:
        source = pipeline_task_source(pipeline_binding(), runtime)
    module = ModuleType('lineage_task_assembled_' + runtime)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, runtime + '.py', 'exec'), module.__dict__)
    request = Mock(return_value=(job(), {}))
    reader = module.LineageTaskReader(request, PLATFORM, WORKSPACE)
    assert reader.visible(task_node())
    assert not reader.visible({**task_node(), 'qualifiedName': URI.replace(WORKSPACE, 'foreign')})
    request.assert_called_once_with('GET', f'/workspaces/{WORKSPACE}/jobs/{JOB}')
