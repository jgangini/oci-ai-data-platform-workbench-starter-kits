from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai_data_governance import activity_program as program
from app.ai_data_governance.activity_commit import activity_receipt,activity_failure_receipt
from app.ai_data_governance.activity_records import validate_activity_batch,activity_typed_rows
from app.ai_data_governance.activity_source import activity_program_source
from app.ai_data_governance.activity_transport import activity_command,run_native_activity
from test_ai_dg_activity import activity_fixture


class Records:
    def __init__(self,config,batch):
        self.rows={'AI_DG_SCOPE':[program.activity_scope(config)],'AI_DG_AGENT':[deepcopy(batch['agent'])]}
        self.writes=[]; self.fail=None
    def read_by_keys(self,name,keys):
        return deepcopy([row for row in self.rows.get(name,[]) if any(all(row[k]==v for k,v in key.items()) for key in keys)])
    def insert_immutable(self,name,rows):
        self.writes.append(name)
        if self.fail==name: raise RuntimeError('fixed write failed')
        current=self.rows.setdefault(name,[])
        for row in rows:
            if row not in current: current.append(deepcopy(row))
    def update_current(self,name,rows,expected):
        assert self.rows[name]==expected
        self.rows[name]=deepcopy(rows); self.writes.append(name)


def native_fixture(monkeypatch):
    config,_,batch=activity_fixture(); config['storage_root']='oci://artifacts@namespace/oci_artifacts'
    repo=Records(config,batch)
    monkeypatch.setattr(program,'DeltaRepository',lambda *a,**kw:repo)
    context=SimpleNamespace(getLocalProperty=lambda key:None,setLocalProperty=lambda *a:None,
        statusTracker=lambda:SimpleNamespace(getJobIdsForGroup=lambda group:[1],getJobInfo=lambda key:SimpleNamespace(status='FAILED')))
    return config,batch,repo,SimpleNamespace(sparkContext=context,conf=SimpleNamespace(set=Mock()))


def test_fixed_program_appends_only_approved_monitoring_rows_and_readback(monkeypatch):
    config,batch,repo,spark=native_fixture(monkeypatch)
    assert program.run_activity_program(spark,config,'APPEND_ACTIVITY',batch)==activity_receipt(batch)
    spark.conf.set.assert_called_once_with('spark.sql.session.timeZone','UTC')
    assert repo.writes==['AI_DG_RUN','AI_DG_EVENT']
    assert program.run_activity_program(spark,config,'VERIFY_ACTIVITY',batch)==activity_receipt(batch)
    assert repo.writes==['AI_DG_RUN','AI_DG_EVENT']
    assert repo.rows['AI_DG_RUN']==activity_typed_rows(batch)['AI_DG_RUN']


@pytest.mark.parametrize('changed',['scope','native_agent','producer_revision','code_path'])
def test_changed_installation_or_producer_fails_before_write(monkeypatch,changed):
    config,batch,repo,spark=native_fixture(monkeypatch)
    if changed=='scope': repo.rows['AI_DG_SCOPE'][0]['workspace_key']='other'
    elif changed=='native_agent': repo.rows['AI_DG_AGENT'][0]['native_agent_key']='other'
    elif changed=='code_path': repo.rows['AI_DG_AGENT'][0]['code_path']='other'
    else:
        config['deployment_stage']='PROD'
        repo.rows['AI_DG_AGENT'][0]['deployment_version']='e'*64
    with pytest.raises(ValueError): program.run_activity_program(spark,config,'APPEND_ACTIVITY',batch)
    assert not repo.writes


def test_development_revision_is_logged_without_mutating_registered_production_revision(monkeypatch):
    config,batch,repo,spark=native_fixture(monkeypatch)
    repo.rows['AI_DG_AGENT'][0]['deployment_version']='e'*64
    assert program.run_activity_program(spark,config,'APPEND_ACTIVITY',batch)==activity_receipt(batch)
    assert repo.rows['AI_DG_AGENT'][0]['deployment_version']=='e'*64
    run=repo.rows['AI_DG_RUN'][0]
    assert run['component_version']=='d'*64 and json.loads(run['manifest_json'])['deployment_stage']=='TEST'
    assert run['run_kind']=='AGENT' and run['attempt_no']==1 and run['status']=='SUCCEEDED'
    assert {e['event_type'] for e in repo.rows['AI_DG_EVENT']}=={'PROGRESS'}
    with pytest.raises(ValueError): program.run_activity_program(spark,config,'REGISTER_AGENT',None)


def test_failed_partial_write_returns_terminal_proof_only_when_own_spark_jobs_have_ended(monkeypatch):
    config,batch,repo,spark=native_fixture(monkeypatch); repo.fail='AI_DG_EVENT'
    assert program.run_activity_program(spark,config,'APPEND_ACTIVITY',batch)==activity_failure_receipt(batch)
    assert repo.rows['AI_DG_RUN'] and not repo.rows.get('AI_DG_EVENT')
    spark.sparkContext.statusTracker=lambda:SimpleNamespace(getJobIdsForGroup=lambda group:[1],getJobInfo=lambda key:None)
    with pytest.raises(RuntimeError,match='uncertain'): program.run_activity_program(spark,config,'APPEND_ACTIVITY',batch)


@pytest.mark.parametrize('state',[None,'RUNNING','UNKNOWN'])
def test_active_or_unavailable_jobs_never_authorize_write_repair(monkeypatch,state):
    config,batch,repo,spark=native_fixture(monkeypatch);repo.fail='AI_DG_EVENT'
    spark.sparkContext.statusTracker=lambda:SimpleNamespace(getJobIdsForGroup=lambda group:[1],
        getJobInfo=lambda key:None if state is None else SimpleNamespace(status=state))
    with pytest.raises(RuntimeError,match='uncertain'): program.run_activity_program(spark,config,'APPEND_ACTIVITY',batch)


def test_empty_tracker_is_not_proof_that_a_failed_write_stopped(monkeypatch):
    config,batch,repo,spark=native_fixture(monkeypatch);repo.fail='AI_DG_EVENT'
    spark.sparkContext.statusTracker=lambda:SimpleNamespace(getJobIdsForGroup=lambda group:[])
    with pytest.raises(RuntimeError,match='uncertain'): program.run_activity_program(spark,config,'APPEND_ACTIVITY',batch)


def test_request_cannot_relabel_a_test_invocation_as_production(monkeypatch):
    config,batch,repo,spark=native_fixture(monkeypatch);config['deployment_stage']='PROD'
    with pytest.raises(ValueError): program.run_activity_program(spark,config,'APPEND_ACTIVITY',batch)
    assert not repo.writes


def test_lost_acknowledgement_can_be_verified_after_a_producer_upgrade_without_old_writes(monkeypatch):
    config,batch,repo,spark=native_fixture(monkeypatch);config['deployment_stage']='PROD'
    manifest=json.loads(batch['run']['manifest_json']);manifest['deployment_stage']='PROD'
    from app.ai_data_governance.inventory import canonical
    batch['run']['manifest_json']=canonical(manifest)
    assert program.run_activity_program(spark,config,'APPEND_ACTIVITY',batch)==activity_receipt(batch)
    repo.rows['AI_DG_AGENT'][0]['deployment_version']='e'*64
    writes=list(repo.writes)
    assert program.run_activity_program(spark,config,'VERIFY_ACTIVITY',batch)==activity_receipt(batch)
    assert repo.writes==writes and repo.rows['AI_DG_AGENT'][0]['deployment_version']=='e'*64
    with pytest.raises(ValueError): program.run_activity_program(spark,config,'APPEND_ACTIVITY',batch)
    assert repo.writes==writes


def test_registration_is_explicit_checks_expected_revision_and_retains_identity(monkeypatch):
    config,batch,repo,spark=native_fixture(monkeypatch); config['deployment_stage']='PROD'
    repo.rows['AI_DG_AGENT'][0]['deployment_version']='e'*64
    with pytest.raises(ValueError): program.run_activity_program(spark,config,'REGISTER_AGENT',None)
    assert not repo.writes
    result=program.run_activity_program(spark,config,'REGISTER_AGENT',None,expected_version='e'*64)
    assert result=={'agent_id':batch['agent']['agent_id'],'deployment_version':'d'*64,'verified':True}
    assert repo.writes==['AI_DG_AGENT']


def test_first_dev_registration_creates_one_logical_producer_without_a_second_test_role(monkeypatch):
    config,batch,repo,spark=native_fixture(monkeypatch);repo.rows['AI_DG_AGENT']=[]
    result=program.run_activity_program(spark,config,'REGISTER_AGENT',None)
    assert result['verified'] is True and repo.rows['AI_DG_AGENT']==[batch['agent']]
    assert repo.rows['AI_DG_AGENT'][0]['agent_role']=='GOVERNANCE_ASSISTANT'
    before=list(repo.writes)
    assert program.run_activity_program(spark,config,'REGISTER_AGENT',None)==result
    assert repo.writes==before


def test_non_utc_start_cannot_hide_an_event_before_the_actual_invocation():
    _,_,batch=activity_fixture()
    batch['run']['started_at']='2026-11-01T00:00:00-05:00'
    batch['run']['finished_at']='2026-11-01T06:00:00.000000+00:00'
    for e in batch['events']: e['ingested_at']=batch['run']['finished_at']
    with pytest.raises(ValueError): validate_activity_batch(batch)


def test_assembled_command_has_no_missing_helpers_or_hidden_loader():
    config,_,batch=activity_fixture()
    source=activity_program_source()
    ns={}; exec(compile(source,'activity.py','exec'),ns)
    assert ns['validate_activity_batch'](batch)==batch
    assert ns['activity_typed_rows'](batch)==activity_typed_rows(batch)
    command=activity_command(config,source,'VERIFY_ACTIVITY',batch)
    compile(command,'activity.py','exec')
    assert 'exec(' not in command and 'base64' not in command


@pytest.mark.parametrize('changed',['failed','truncated','encoded','extra','error','wrong_type'])
def test_native_adapter_requires_one_complete_successful_fixed_program_receipt(monkeypatch,changed):
    from app.ai_data_governance import activity_transport
    config,_,batch=activity_fixture()
    result={'status':'SUCCEEDED','outputs':[{'type':'TEXT_PLAIN','value':json.dumps(activity_receipt(batch))}]}
    if changed=='failed': result['status']='FAILED'
    if changed=='truncated': result['results_truncated']=True
    if changed=='encoded': result['outputs'][0]['is_base64']=True
    if changed=='extra': result['outputs']*=2
    if changed=='error': result['error']={'message':'hidden'}
    if changed=='wrong_type': result['outputs'][0]['type']='OTHER'
    monkeypatch.setattr(activity_transport,'_run_central_command',lambda *a,**kw:result)
    with pytest.raises((ValueError,RuntimeError)): run_native_activity(config,None,'fixed','APPEND_ACTIVITY',batch,lambda:None)
