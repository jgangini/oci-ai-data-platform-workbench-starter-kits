"""Disposable Spark 3.5/Delta 3.2 Sonda acceptance; synthetic fixtures only.

First run with the backend Python environment:
  python scripts/check_sonda_delta.py --prepare .tmp/sonda-delta-input.json
Then spark-submit this script with --input and that JSON mounted read-only.
Preparation validates every derived fixture through the authoritative contract;
the Spark image needs no Pydantic/HTTP/LLM dependencies. No cloud credentials.
"""
import argparse
import copy
import importlib.util
import json
from datetime import timedelta
from pathlib import Path
import tempfile
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]


def prepare(path):
    import sys
    sys.path.insert(0, str(ROOT / 'apps/backend'))
    from app import sonda_contract as c
    from app.labs.ai_social_network.notebooks import sonda_delta as d
    manifest = json.loads((ROOT / 'contracts/sonda/v1/manifest.json').read_text())
    def valid(value):
        return c.parse(c.encoded(value), registry_id=UUID(int=1), generation_id=UUID(int=2),
                       contract_sha256=manifest['contract_sha256']).model_dump(mode='json')
    def fixture(name):
        return valid(json.loads((ROOT / 'contracts/sonda/v1/fixtures' / name).read_text()))
    config = fixture('01-configuration.json')
    opened = fixture('09-budget-round-open.json')
    observed = fixture('02-observations.json')
    multi = copy.deepcopy(observed)
    multi['message_id'] = str(UUID(int=700))
    multi['payload'].update(query_id=config['payload']['queries'][1]['query_id'], execution_id=str(UUID(int=701)))
    for row in multi['payload']['observations']:
        row['observed_at'] = '2026-01-01T00:05:00Z'
    changed = copy.deepcopy(observed)
    changed['message_id'] = str(UUID(int=702))
    changed['payload']['execution_id'] = str(UUID(int=703))
    changed['payload']['observations'] = [changed['payload']['observations'][0]]
    row = changed['payload']['observations'][0]
    row.update(observed_at='2026-01-01T00:20:00Z', published_at='2025-12-31T00:00:00Z')
    row['metrics']['comments'] = 5
    row.update(c.observation_fingerprints(row))
    late = copy.deepcopy(observed)
    late['message_id'] = str(UUID(int=704))
    late['payload']['execution_id'] = str(UUID(int=705))
    late['payload']['observations'] = [late['payload']['observations'][0]]
    late['payload']['observations'][0]['observed_at'] = '2026-01-01T00:10:00Z'
    rejected = fixture('08-rejected-configuration.json')
    approved = copy.deepcopy(rejected)
    approved['message_id'] = str(UUID(int=706))
    approved['payload'].update(revision=3, previous_revision=2)
    criterion = approved['payload']['criteria'][0]
    criterion.update(revision=2, origin='agent', proposal_id=str(UUID(int=707)), author_id='synthetic-agent', threshold=0.85)
    approved['payload']['proposal_decisions'] = [{'proposal_id': str(UUID(int=707)), 'decision': 'approved',
        'operator_id': criterion['approval']['decided_by'], 'decided_at': criterion['approval']['decided_at'],
        'config_revision': 3, 'reason': 'Synthetic operator approval'}]
    missing = copy.deepcopy(observed)
    missing['message_id'] = str(UUID(int=708))
    missing['payload']['source_revision'] = 99  # structurally valid, missing Gold FK
    stale_budget = copy.deepcopy(opened)
    stale_budget['message_id'] = str(UUID(int=709))
    conflict = copy.deepcopy(observed)
    conflict['created_at'] = '2026-01-01T00:40:00Z'  # same message ID, changed DTO
    unknown_budget = copy.deepcopy(opened)
    unknown_budget['message_id'] = str(UUID(int=710))
    unknown_budget['payload'].update(budget_round_id=str(UUID(int=711)), remaining_microusd=None,
                                    research_remaining_microusd=None, remaining_items=None)
    refusal = fixture('04-execution-result.json')
    refusal['payload']['request_id'] = None
    values = {'configuration': config, 'opened': opened, 'observations': observed,
        'multi': valid(multi), 'changed': valid(changed), 'late': valid(late),
        'rejected': rejected, 'approved': valid(approved), 'missing_reference': valid(missing),
        'closed': fixture('10-budget-round-closed.json'), 'stale_budget': valid(stale_budget),
        'conflict': valid(conflict), 'unknown_budget': valid(unknown_budget),
        'execution_refusal': valid(refusal)}
    case_id = d.observation_rows(observed, config['payload'])['investigations'][0]['investigation_id']
    report = fixture('05-report.json')['payload']
    report.update(investigation_id=case_id, report_id=d.fingerprint([case_id, 'report']))
    report = c.Report.model_validate(report).model_dump(mode='json')
    request = fixture('03-investigation-request.json')['payload']
    request['investigation_id'] = case_id
    request = c.InvestigationRequest.model_validate(request).model_dump(mode='json')
    proposal = fixture('06-proposal.json')['payload']
    proposal['investigation_id'] = case_id
    proposal = c.Proposal.model_validate(proposal).model_dump(mode='json')
    state = {'scope': {key: config[key] for key in ('registry_id', 'generation_id', 'contract_version', 'contract_sha256')},
             'evaluation_cases': {e['evaluation_key']: case_id for e in report['evaluations']}}
    studies = {'first': {'evaluations': report['evaluations'], 'reports': [report], 'investigation_requests': [request], 'proposals': [proposal], 'state': state}}
    second = copy.deepcopy(studies['first'])
    second.update(investigation_requests=[], proposals=[])
    for evaluation in second['evaluations']:
        evaluation['model_revision'] = 'synthetic-second-model-version'
        evaluation['evaluation_key'] = c.evaluation_fingerprint(evaluation)
        evaluation['evaluated_at'] = '2026-01-01T00:35:00Z'
        c.Evaluation.model_validate(evaluation)
        second['state']['evaluation_cases'][evaluation['evaluation_key']] = case_id
    second['reports'][0].update(revision=2, model_revision='synthetic-second-model-version', created_at='2026-01-01T00:35:00Z')
    second['reports'][0] = c.Report.model_validate(second['reports'][0]).model_dump(mode='json')
    studies['second'] = second
    case_evaluation = copy.deepcopy(report['evaluations'][0])
    changed_observation = changed['payload']['observations'][0]
    case_evaluation.update(observation_key=changed_observation['observation_key'], model_revision='synthetic-case-model-v1', evaluated_at='2026-01-01T00:40:00Z')
    case_evaluation['evidence'] = [{**case_evaluation['evidence'][0], 'observation_key': changed_observation['observation_key'],
        'document_key': changed_observation['document_key'], 'captured_at': changed_observation['observed_at'],
        'evidence_id': c.sha256(c.encoded([changed_observation['observation_key'], case_evaluation['evidence'][0]['quote']]))}]
    case_evaluation['evaluation_key'] = c.evaluation_fingerprint(case_evaluation)
    case_evaluation = c.Evaluation.model_validate(case_evaluation).model_dump(mode='json')
    from check_sonda_chat_sql import prepare_chat_sql_checks
    output = {'messages': values, 'studies': studies, 'case_evaluation': case_evaluation,
              'chat_sql': prepare_chat_sql_checks(values)}
    target = Path(path)
    if not target.parent.is_dir():
        target.parent.mkdir(parents=True)
    target.write_text(canonical(output), encoding='utf-8')
    print('Prepared ' + str(len(values)) + ' validated synthetic messages, 2 compatibility studies and one typed production-case evaluation')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n'


def main(path):
    from pyspark.sql import SparkSession
    spec = importlib.util.spec_from_file_location('sonda_delta', ROOT / 'apps/backend/app/labs/ai_social_network/notebooks/sonda_delta.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    inputs = json.loads(Path(path).read_text(encoding='utf-8'))
    messages, studies = inputs['messages'], inputs['studies']
    assert all(v['contract_sha256'] == m.CONTRACT_SHA256 for v in messages.values())
    scratch = Path(tempfile.mkdtemp(prefix='sonda-delta-synthetic-'))
    spark = (SparkSession.builder.master('local[2]').appName('sonda-delta-synthetic')
        .config('spark.ui.enabled', 'false').config('spark.sql.shuffle.partitions', '2')
        .config('spark.databricks.delta.snapshotPartitions', '2')
        .config('spark.sql.extensions', 'io.delta.sql.DeltaSparkSessionExtension')
        .config('spark.sql.catalog.spark_catalog', 'org.apache.spark.sql.delta.catalog.DeltaCatalog')
        .config('spark.sql.warehouse.dir', str(scratch / 'warehouse')).getOrCreate())
    spark.sparkContext.setLogLevel('ERROR')
    spark.conf.set('spark.sql.session.timeZone', 'UTC')
    sql=spark.sql
    metadata_reads=[]
    def measured_sql(query,*args,**kwargs):
        if query.startswith('DESCRIBE DETAIL '):
            metadata_reads.append(query)
        return sql(query,*args,**kwargs)
    spark.sql=measured_sql
    binding = {'catalog': 'spark_catalog', 'gold_schema': 'sonda_gold', 'control_schema': 'sonda_control',
        'namespace': 'synthetic', 'source_bucket': 'fixture_source', 'gold_bucket': 'fixture_gold',
        'artifacts_bucket': 'fixture_artifacts', 'registry_id': str(UUID(int=1)),
        'generation_id': str(UUID(int=2)), 'contract_sha256': m.CONTRACT_SHA256}
    # Replace only the Python resolver inside this disposable process. Production
    # binding never accepts file/PAR/object-location overrides.
    m.location = lambda b, name, layer: (scratch / layer / name).as_uri()
    m.clock = lambda: m.utc('2026-01-01T00:30:00Z')
    def table(suffix):
        return m._table(binding, suffix)
    def count(suffix):
        return spark.table(table(suffix)).count()
    try:
        m.ensure_tables(spark, binding)
        assert len(metadata_reads)==len(m.definitions(binding))==26
        assert all(count(suffix) == 0 for suffix in ('document_current', 'observations', 'reports', 'outbox'))
        first = m.apply_messages(spark, binding, [messages[k] for k in ('configuration', 'opened', 'observations')])
        assert {r['state'] for r in first['receipts']} == {'APPLIED'}
        assert count('document_current') == count('observations') == count('discoveries') == 2
        assert count('evaluations') == 2 and count('investigations') == 1
        assert spark.table(table('document_current')).where('comments IS NOT NULL').count() == 0
        assert spark.table(table('evaluations')).where('score IS NOT NULL OR model_id IS NOT NULL').count() == 0
        assert sum(r.document_count for r in spark.table(table('daily')).collect()) == 2
        # Typed study output is durably published before any wire outbox entry.
        study_first = m.apply_study_results(spark, binding, studies['first'], result_id='synthetic-study-1')
        assert study_first['state'] == 'APPLIED'
        assert count('evaluation_results') == 2 and count('evidence') == 2 and count('reports') == 1
        assert count('requests') == 1 and count('proposals') == 1
        assert spark.table(table('evaluations')).where("state='completed'").count() == 2
        assert spark.table(table('outbox')).where("status='ready'").count() == 3
        assert m.apply_study_results(spark, binding, studies['first'], result_id='synthetic-study-1')['publication']['publication_id'] == study_first['publication']['publication_id']
        # A new model revision appends final identities; interrupted report/state
        # writes remain invisible to publication and replay without duplicates.
        original_rows = m._rows
        def study_interrupt(spark_arg, b, suffix, rows, *args, **kwargs):
            if suffix == 'reports':
                raise RuntimeError('Synthetic interruption before study report')
            return original_rows(spark_arg, b, suffix, rows, *args, **kwargs)
        m._rows = study_interrupt
        try:
            m.apply_study_results(spark, binding, studies['second'], result_id='synthetic-study-2')
            raise AssertionError('Study fault injection did not execute')
        except RuntimeError as exc:
            assert 'Synthetic interruption' in str(exc)
        finally:
            m._rows = original_rows
        assert count('evaluation_results') == 4 and count('reports') == 1
        assert m._find(spark, binding, 'study_results', result_id='synthetic-study-2')[0]['state'] == 'APPLYING'
        old_versions = json.loads(study_first['publication']['versions_json'])
        assert spark.sql('SELECT count(*) n FROM ' + table('evaluation_results') + ' VERSION AS OF ' + str(old_versions['ai_social_evaluation_results'])).first()['n'] == 2
        m.apply_messages(spark, binding, [])
        assert count('evaluation_results') == 4 and count('reports') == 2 and count('evaluations') == 2
        assert m._find(spark, binding, 'study_state')[0]['sequence'] == 2
        assert spark.table(table('outbox')).where("kind='report' AND status='ready'").count() == 2
        first['publication'] = m._find(spark, binding, 'publication')[0]
        before = first['publication']
        repeated = m.apply_messages(spark, binding, [messages['observations']])
        assert repeated['publication']['publication_id'] == before['publication_id']
        assert count('discoveries') == 2
        # Fail after observation/provenance/evaluation commits, before current.
        original_rows = m._rows
        def interrupt(spark_arg, b, suffix, rows, *args, **kwargs):
            if suffix == 'document_current':
                raise RuntimeError('Synthetic interruption before current commit')
            return original_rows(spark_arg, b, suffix, rows, *args, **kwargs)
        m._rows = interrupt
        try:
            m.apply_messages(spark, binding, [messages['changed']])
            raise AssertionError('Fault injection did not execute')
        except RuntimeError as exc:
            assert 'Synthetic interruption' in str(exc)
        finally:
            m._rows = original_rows
        assert count('observations') == 3
        assert m._find(spark, binding, 'receipts', message_id=messages['changed']['message_id'])[0]['state'] == 'APPLYING'
        assert m._find(spark, binding, 'publication')[0]['publication_id'] == before['publication_id']
        versions = json.loads(before['versions_json'])
        assert spark.sql('SELECT count(*) n FROM ' + table('observations') + ' VERSION AS OF ' + str(versions['ai_social_observations'])).first()['n'] == 2
        try:
            m.publish(spark, binding)
            raise AssertionError('Dirty writer must block publication')
        except RuntimeError as exc:
            assert 'replayed' in str(exc)
        m.apply_messages(spark, binding, [])
        assert count('observations') == 3 and count('document_current') == 2
        key = messages['changed']['payload']['observations'][0]['document_key']
        assert m._find(spark, binding, 'document_current', document_key=key)[0]['comments'] == 5
        # Multi-query duplicate, late unchanged capture, rejected and approved
        # proposals, unknown budget, and stale budget after a terminal close.
        final = m.apply_messages(spark, binding, [messages[k] for k in ('multi', 'rejected', 'approved', 'late',
            'closed', 'stale_budget', 'unknown_budget', 'execution_refusal', 'missing_reference')])
        assert final['receipts'][-1]['state'] == 'REJECTED'
        assert count('document_current') == 2 and count('observations') == 3 and count('discoveries') == 6
        assert count('evaluations') == 3 and count('investigations') == 1
        current = m._find(spark, binding, 'document_current', document_key=key)[0]
        assert current['comments'] == 5 and str(current['published_date']) == '2025-12-31'
        assert sum(r.document_count for r in spark.table(table('daily')).collect()) == 2
        assert sum(r.comments or 0 for r in spark.table(table('daily')).collect()) == 5
        assert m._find(spark, binding, 'budget_rounds', budget_round_id=messages['opened']['payload']['budget_round_id'])[0]['status'] == 'closed'
        unknown = m._find(spark, binding, 'budget_rounds', budget_round_id=messages['unknown_budget']['payload']['budget_round_id'])[0]
        assert unknown['remaining_items'] is None and unknown['remaining_microusd'] is None
        assert count('registry') == 3
        assert {r.state for r in spark.table(table('proposals')).collect()} == {'approved', 'rejected'}
        assert count('reports') == 2 and count('evidence') == 2
        versions = json.loads(final['publication']['versions_json'])
        assert len(versions) == len(m.definitions(binding)) - 5
        try:
            m.apply_messages(spark, binding, [messages['conflict']])
            raise AssertionError('Changed message identity was accepted')
        except ValueError as exc:
            assert 'Conflicting message identity' in str(exc)
        bad_binding = dict(binding, generation_id=str(UUID(int=999)))
        try:
            m.apply_messages(spark, bad_binding, [messages['observations']])
            raise AssertionError('Cross-generation message was accepted')
        except ValueError as exc:
            assert 'scope' in str(exc)
        # The production case API does not read or overwrite the compatibility
        # generation state. Each bounded artifact is JSON ready for the worker.
        case_id = studies['first']['reports'][0]['investigation_id']
        generation_state_before = m._find(spark, binding, 'study_state')[0]['state_json']
        selected = m.list_pending_cases(spark, binding, limit=5)
        assert any(r['investigation_id'] == case_id and r['phase'] == 'evaluate' for r in selected['cases'])
        hydrated = m.load_case_work(spark, binding, case_id)
        work = hydrated['work']
        assert work['phase'] == 'evaluate' and len(work['pending_inputs']) == 1 and hydrated['state'] is None
        assert work['pending_inputs'][0]['observation']['observation_key'] == inputs['case_evaluation']['observation_key']
        state = {'scope': work['scope'], 'investigation_id': case_id, 'version': 1, 'predecessor_revision': 0,
            'phase': 'evaluate', 'input_revision': work['input_revision'], 'last_input_fingerprint': m.fingerprint(work),
            'report_revision': work['report_revision'], 'request_ids': [r['request']['request_id'] for r in work['requests']], 'proposal_ids': []}
        case_result = {'state': state, 'evaluations': [inputs['case_evaluation']], 'reports': [], 'proposals': [], 'investigation_requests': []}
        evaluated = m.apply_case_study_results(spark, binding, case_result, result_id='synthetic-case-evaluate-1', predecessor_revision=0)
        assert evaluated['state'] == 'APPLIED'
        assert m._find(spark, binding, 'study_case_state', investigation_id=case_id)[0]['version'] == 1
        assert m._find(spark, binding, 'study_state')[0]['state_json'] == generation_state_before
        assert m.apply_case_study_results(spark, binding, case_result, result_id='synthetic-case-evaluate-1', predecessor_revision=0)['publication']['publication_id'] == evaluated['publication']['publication_id']
        stale = copy.deepcopy(case_result)
        stale['state']['last_input_fingerprint'] = 'f' * 64
        try:
            m.apply_case_study_results(spark, binding, stale, result_id='synthetic-case-stale', predecessor_revision=0)
            raise AssertionError('Stale predecessor replaced case state')
        except ValueError as exc:
            assert 'Stale case predecessor' in str(exc)
        assert count('case_study_results') == 1
        report_package = m.load_case_work(spark, binding, case_id)
        report_work = report_package['work']
        assert report_work['phase'] == 'report' and report_work['facts'] == {
            'distinct_documents': 2, 'distinct_observations': 3, 'relevant_documents': 1, 'missing_evaluations': 0}
        assert len(report_work['evaluations']) == len(report_work['evidence']) == 3
        assert next(r for r in report_work['indicators'] if r['key'] == 'comments')['value'] == '5'
        report = copy.deepcopy(studies['second']['reports'][0])
        report.update(revision=report_work['report_revision'] + 1, title='Synthetic production case',
            summary='Local synthetic case using complete Gold facts; no model or Actor was invoked.',
            model_revision='synthetic-case-model-v1', evidence=report_work['evidence'], evaluations=report_work['evaluations'], indicators=report_work['indicators'],
            status='awaiting_evidence', created_at='2026-01-01T00:45:00Z')
        report['coverage'].update(distinct_documents=2, distinct_observations=3, query_ids=report_work['query_ids'],
            pending_requests=1, failed_requests=0, exhausted_budget=False, limitations=['Synthetic local acceptance only.'])
        final_state = {**state, 'version': 2, 'predecessor_revision': 1, 'phase': 'report',
            'input_revision': report_work['input_revision'], 'last_input_fingerprint': m.fingerprint(report_work), 'report_revision': report['revision']}
        report_result = {'state': final_state, 'evaluations': [], 'reports': [report], 'proposals': [], 'investigation_requests': []}
        before_case_report = m._find(spark, binding, 'publication')[0]
        original_rows = m._rows
        def interrupt_case(spark_arg, b, suffix, rows, *args, **kwargs):
            if suffix == 'reports':
                raise RuntimeError('Synthetic interruption after case CAS before report')
            return original_rows(spark_arg, b, suffix, rows, *args, **kwargs)
        m._rows = interrupt_case
        try:
            m.apply_case_study_results(spark, binding, report_result, result_id='synthetic-case-report-1', predecessor_revision=1)
            raise AssertionError('Case fault injection did not execute')
        except RuntimeError as exc:
            assert 'Synthetic interruption' in str(exc)
        finally:
            m._rows = original_rows
        assert m._find(spark, binding, 'case_study_results', result_id='synthetic-case-report-1')[0]['state'] == 'APPLYING'
        old_version = json.loads(before_case_report['versions_json'])['ai_social_study_case_state']
        assert spark.sql('SELECT version FROM ' + table('study_case_state') + ' VERSION AS OF ' + str(old_version)).first()['version'] == 1
        m.apply_messages(spark, binding, [])
        assert m._find(spark, binding, 'study_case_state', investigation_id=case_id)[0]['version'] == 2
        assert not m._find(spark, binding, 'case_work', investigation_id=case_id)[0]['dirty']
        assert m._find(spark, binding, 'study_state')[0]['state_json'] == generation_state_before
        assert m.apply_case_study_results(spark, binding, report_result, result_id='synthetic-case-report-1', predecessor_revision=1)['state'] == 'APPLIED'
        noop = {**report_result, 'reports': []}
        assert m.apply_case_study_results(spark, binding, noop, result_id='synthetic-case-noop', predecessor_revision=1)['no_op']
        # One thousand unrelated cases cannot enlarge this case's hydration or
        # its fingerprint. A fixed pending-case limit still bounds driver work.
        other_cases, others = [], []
        criterion = work['criterion']
        for i in range(1000):
            start = m.utc('2020-01-01T00:00:00Z') + timedelta(days=i)
            other_id = m.fingerprint([criterion['criterion_id'], criterion['revision'], criterion['case_scope_key'], m.json_mode(start)])
            other_cases.append({'investigation_id': other_id, 'criterion_id': criterion['criterion_id'],
                'criterion_revision': criterion['revision'], 'config_revision': work['config_revision'],
                'case_scope_key': criterion['case_scope_key'], 'window_start': start, 'state': 'awaiting_evaluation'})
            others.append({'investigation_id': other_id, 'input_revision': m.fingerprint(['signal', i]), 'dirty': True, 'updated_at': m.clock()})
        m._rows(spark, binding, 'investigations', other_cases, ('investigation_id',))
        m._rows(spark, binding, 'case_work', others, ('investigation_id',), update=True)
        # Publish an ordinary safe rejection to pin those disposable work rows.
        rejection = m.reject_transport_message(spark, binding, message_id=str(UUID(int=900)), raw_sha256='e' * 64,
            kind='observations', code='invalid_business', source_sequence=99)
        assert rejection['receipt']['state'] == 'REJECTED' and rejection['receipt']['message_json'] == 'null'
        receipt_outbox = m._find(spark, binding, 'outbox', source_message_id=str(UUID(int=900)))[0]
        wire = json.loads(receipt_outbox['payload_json'])
        assert receipt_outbox['status'] == 'ready' and wire['payload']['source_sequence'] == 99 and wire['payload']['status'] == 'rejected'
        limited = m.list_pending_cases(spark, binding, limit=5)
        assert len(limited['cases']) == 5 and all(r['investigation_id'] != case_id for r in limited['cases'])
        isolated = m.load_case_work(spark, binding, case_id)
        assert isolated['work']['facts'] == report_work['facts']
        assert isolated['work']['input_revision'] == report_work['input_revision']
        assert isolated['state']['version'] == 2
        assert len(m.canonical(isolated['state']).encode('utf-8')) < 4096
        assert 'Northstar' not in m.canonical(isolated['state'])
        from check_sonda_chat_sql import run_chat_sql_checks
        run_chat_sql_checks(spark,m,binding,inputs['chat_sql'])
        assert len(metadata_reads)==26, 'Writes must reuse the initially validated bound paths'
        print('PASS: real Spark/Delta Sonda input/study replay, exact versions, production per-case CAS/state, bounded 1000-other-case isolation, full SQL facts and safe transport rejection')
    finally:
        spark.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--prepare')
    choice.add_argument('--input')
    args = parser.parse_args()
    if args.prepare:
        prepare(args.prepare)
    else:
        main(args.input)
