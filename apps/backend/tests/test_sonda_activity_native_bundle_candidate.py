"""Offline native-source parity; only reviewed canonical Activity source executes."""
import ast
import asyncio
import builtins
import copy
import importlib.util
import json
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT/'contracts/sonda-activity/candidate-v1'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, CONTRACT/filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = load('activity_bundle_builder_tests', 'native_bundle.py')
reader = load('activity_bundle_read_tests', 'reference.py')
producer = load('activity_bundle_producer_tests', 'producer.py')
public = load('activity_bundle_public_tests', 'public_stream.py')
journal = load('activity_bundle_journal_tests', 'journal.py')
results = load('activity_bundle_results_tests', 'results.py')
RESULTS_SOURCE = (CONTRACT/'results.py').read_text(encoding='utf-8')
JOURNAL_SOURCE = (CONTRACT/'journal.py').read_text(encoding='utf-8')
SOURCES = {name: (CONTRACT/filename).read_text(encoding='utf-8') for name, filename in
    [('read','reference.py'), ('producer','producer.py'), ('public','public_stream.py')]}
WIRE_FIXTURES = {path.name: path.read_bytes() for path in (CONTRACT/'fixtures').glob('*.json')}


def no_file_io(*args, **kwargs):
    raise AssertionError('Native Activity codec must not perform file IO')


@pytest.fixture(params=[3, 4, 5], ids=['three-source', 'with-journal', 'with-results'])
def native(monkeypatch, request):
    inputs = dict(SOURCES)
    if request.param >= 4:
        inputs['journal_source'] = JOURNAL_SOURCE
    if request.param >= 5:
        inputs['results_source'] = RESULTS_SOURCE
    source = builder.bundle_source(**inputs)
    names = builder.binding_map(**inputs)
    module = ModuleType('activity_native_compiled_tests')
    sentinels = {name: object() for name in ('Scope','Run','parse','Closed','Hash64','PRODUCER_VERSION','ReportRef','NativeStep','ResultEnvelope','RESULTS','Count')}
    module.__dict__.update(sentinels)
    monkeypatch.setitem(sys.modules,module.__name__,module)
    with monkeypatch.context() as guarded:
        guarded.setattr(builtins,'open',no_file_io)
        guarded.setattr(Path,'open',no_file_io)
        exec(compile(source,'<reviewed-activity-bundle>','exec'),module.__dict__)
    assert all(module.__dict__[name] is original for name,original in sentinels.items())
    return module.__dict__, names


def native_symbol(native, label, name):
    namespace, names = native
    return namespace[names[label][name]]


def test_generation_is_deterministic_lf_and_does_not_read_or_change_core_files(monkeypatch):
    core = ROOT/'contracts/sonda/v1'
    files = [core/name for name in ('reference.py','schema.json','chat.schema.json','manifest.json')]
    before = {path:path.read_bytes() for path in files}
    with monkeypatch.context() as guarded:
        guarded.setattr(builtins,'open',no_file_io)
        guarded.setattr(Path,'open',no_file_io)
        first = builder.bundle_source(**SOURCES)
        assert first == builder.bundle_source(**SOURCES)
        windows = {label:source.replace('\n','\r\n') for label,source in SOURCES.items()}
        assert first == builder.bundle_source(**windows)
    assert '\r' not in first
    assert before == {path:path.read_bytes() for path in files}


def test_all_owned_bindings_are_namespaced_including_aliases_and_constants(native):
    namespace,names = native
    for label in names:
        prefix = builder.PREFIXES[label]
        assert names[label]
        for original,native_name in names[label].items():
            assert native_name == prefix+original and native_name in namespace
    assert names['producer']['Scope'] != names['read']['Scope']
    assert namespace[names['producer']['Scope']] is namespace[names['read']['Scope']]
    descriptor = namespace[names['producer']['AdmissionDescriptor']]
    assert descriptor.model_fields['scope'].annotation is namespace[names['read']['Scope']]
    assert descriptor.model_fields['run'].annotation is namespace[names['read']['Run']]


def test_native_ast_has_no_runtime_loader_or_ambient_filesystem_names():
    tree = ast.parse(builder.bundle_source(**SOURCES))
    for node in ast.walk(tree):
        if isinstance(node,ast.Name):
            assert node.id not in builder.FORBIDDEN_NAMES | {'_read'}
        if isinstance(node,ast.Import):
            assert all(alias.name not in ('importlib','importlib.util','sys','os','pathlib') for alias in node.names)
        if isinstance(node,ast.ImportFrom):
            assert node.module not in ('importlib','importlib.util','pathlib','os','sys')


READ_VALID = [name for name in WIRE_FIXTURES if name.startswith(('runs','events'))]
PRODUCER_VALID = [name for name in WIRE_FIXTURES if name.startswith('producer-')]
REJECTED = [name for name in WIRE_FIXTURES if name.startswith('reject-')]


@pytest.mark.parametrize('name',READ_VALID)
def test_all_valid_read_fixtures_have_identical_serialized_wire_models(native,name):
    raw = WIRE_FIXTURES[name]
    expected = reader.parse(raw).model_dump(mode='json')
    actual = native_symbol(native,'read','parse')(raw).model_dump(mode='json')
    assert actual == expected
    assert producer.producer_encoded(actual) == producer.producer_encoded(expected)


@pytest.mark.parametrize('name',PRODUCER_VALID)
def test_all_valid_producer_fixtures_have_exact_canonical_utf8_parity(native,name):
    raw = WIRE_FIXTURES[name]
    expected = producer.parse_producer_document(raw).model_dump(mode='json')
    actual = native_symbol(native,'producer','parse_producer_document')(raw).model_dump(mode='json')
    assert actual == expected
    assert native_symbol(native,'producer','producer_encoded')(actual) == producer.producer_encoded(expected) == raw


@pytest.mark.parametrize('name',REJECTED)
def test_existing_rejection_fixtures_stay_rejected_in_native_code(native,name):
    raw = WIRE_FIXTURES[name]
    value = json.loads(raw)
    label = 'producer' if str(value.get('schema_version','')).startswith('activity.producer.') else 'read'
    canonical = producer.parse_producer_document if label == 'producer' else reader.parse
    exported = 'parse_producer_document' if label == 'producer' else 'parse'
    with pytest.raises(ValueError):
        canonical(raw)
    with pytest.raises(ValueError):
        native_symbol(native,label,exported)(raw)


@pytest.mark.parametrize('mutation', ['authority','bool_sequence','sequence_gap','utf8_limit','cross_scope','duplicate_json'])
def test_producer_bounds_reserved_fields_and_scope_binding_survive_namespace_generation(native,mutation):
    value = json.loads(WIRE_FIXTURES['producer-batch.json'])
    if mutation == 'authority':
        value['events'][0]['authority'] = 'native_agent'
    elif mutation == 'bool_sequence':
        value['events'][0]['producer_sequence'] = True
    elif mutation == 'sequence_gap':
        value['events'][1]['producer_sequence'] = 8
    elif mutation == 'utf8_limit':
        value['events'][2]['public_text_delta'] = 'Ã°Å¸Ëœâ‚¬'*4096
    elif mutation == 'cross_scope':
        value['scope']['generation_id'] = '00000000-0000-0000-0000-000000000999'
        batch_native = native_symbol(native,'producer','JournalBatch').model_validate(value)
        descriptor = json.loads(WIRE_FIXTURES['producer-descriptor.json'])
        with pytest.raises(ValueError):
            producer.validate_batch_binding(value,descriptor)
        with pytest.raises(ValueError):
            native_symbol(native,'producer','validate_batch_binding')(batch_native,descriptor)
        return
    raw = producer.producer_encoded(value)
    if mutation == 'duplicate_json':
        raw = raw.replace(b'"first_sequence":1',b'"first_sequence":1,"first_sequence":1')
    for parse in (producer.parse_producer_document,native_symbol(native,'producer','parse_producer_document')):
        with pytest.raises(ValueError):
            parse(raw)


def test_full_batch_utf8_byte_limit_and_control_canonical_bytes_match(native):
    value = json.loads(WIRE_FIXTURES['producer-batch.json'])
    text = copy.deepcopy(value['events'][2])
    value['events'] = []
    for sequence in range(1,101):
        event = copy.deepcopy(text)
        event.update(producer_sequence=sequence,event_id=f'00000000-0000-0000-0000-{sequence:012d}',public_text_delta='Ã¦â€“â€¡'*4000)
        value['events'].append(event)
    value.update(first_sequence=1,last_sequence=100)
    for parse in (producer.parse_producer_document,native_symbol(native,'producer','parse_producer_document')):
        with pytest.raises(ValueError):
            parse(producer.producer_encoded(value))
        with pytest.raises(ValueError):
            parse(b' '+WIRE_FIXTURES['producer-descriptor.json'])


def test_admission_claim_and_journal_pure_bindings_match(native):
    descriptor = json.loads(WIRE_FIXTURES['producer-descriptor.json'])
    current = json.loads(WIRE_FIXTURES['producer-current.json'])
    claim = json.loads(WIRE_FIXTURES['producer-claim.json'])
    batch = WIRE_FIXTURES['producer-batch.json']
    index = WIRE_FIXTURES['producer-index.json']
    head = json.loads(WIRE_FIXTURES['producer-head.json'])
    from datetime import datetime,timedelta
    args = {'scope':descriptor['scope'],'agent_id':descriptor['run']['agent_id'],
        'admission_id':descriptor['admission_id'],'native_session_sha256':descriptor['native_session_sha256'],
        'input_sha256':descriptor['input_sha256'],'now':datetime.fromisoformat(descriptor['created_at'].replace('Z','+00:00'))+timedelta(seconds=1)}
    native_admit = native_symbol(native,'producer','validate_admission')
    assert native_admit(descriptor,current,**args).model_dump(mode='json') == producer.validate_admission(descriptor,current,**args).model_dump(mode='json')
    for validate in (native_admit,producer.validate_admission):
        with pytest.raises(ValueError):
            validate(descriptor,current,existing_claim=claim,**args)
    native_claim = native_symbol(native,'producer','validate_claim_binding')(claim,descriptor,owner_nonce=claim['owner_nonce'])
    assert native_claim.model_dump(mode='json') == producer.validate_claim_binding(claim,descriptor,owner_nonce=claim['owner_nonce']).model_dump(mode='json')
    index_value = json.loads(index)
    assert native_symbol(native,'producer','validate_index_batch')(index_value,batch).model_dump(mode='json') == producer.validate_index_batch(index_value,batch).model_dump(mode='json')
    assert native_symbol(native,'producer','validate_head_successor')(None,head,index) == producer.validate_head_successor(None,head,index) == 'create'


class Snapshots:
    def __init__(self,values):
        self.values,self.calls = values,0

    def astream(self,messages):
        self.calls += 1
        async def generate():
            for value in self.values:
                yield value
        return generate()

    async def ainvoke(self,*args,**kwargs):
        raise AssertionError('No second model call or fallback')


async def public_result(function,values,field='answer',failing=False):
    runnable = Snapshots(values)
    deltas,degraded = [],[]
    async def delta(text):
        if failing:
            raise OSError('Synthetic telemetry delivery failure')
        deltas.append(text)
    async def notice(code):
        degraded.append(code)
    result = await function(runnable,['unchanged-message'],field=field,on_delta=delta,on_degraded=notice,max_delta_bytes=4)
    return result,deltas,degraded,runnable.calls


@pytest.mark.parametrize('values,field,failing', [
    ([{}, {'answer':'Ho'}, {'answer':'Hola'}, {'answer':'Hola Ã°Å¸Ëœâ‚¬','private_plan':'never streamed'}],'answer',False),
    ([{'answer':'original'},{'answer':'rewritten'}],'answer',False),
    ([{'summary':'Resumen'},{'summary':'Resumen breve'}],'summary',False),
    ([{'answer':'Hola'},{'answer':'Hola final'}],'answer',True),
])
def test_public_helper_retains_one_generation_deltas_and_degradation_parity(native,values,field,failing):
    actual = asyncio.run(public_result(native_symbol(native,'public','stream_public_field'),values,field,failing))
    expected = asyncio.run(public_result(public.stream_public_field,values,field,failing))
    assert actual == expected and actual[-1] == 1
    assert all(len(text.encode('utf-8')) <= 4 for text in actual[1])
    assert 'never streamed' not in ''.join(actual[1])


def test_builder_rejects_local_shadowing_and_undeclared_core_global_capture():
    bad = dict(SOURCES)
    bad['public'] += '\ndef broken(stream_public_field):\n    return stream_public_field\n'
    with pytest.raises(ValueError,match='shadows'):
        builder.bundle_source(**bad)
    bad = dict(SOURCES)
    bad['public'] += '\ndef broken():\n    return Scope\n'
    with pytest.raises(ValueError,match='ambient global'):
        builder.bundle_source(**bad)


@pytest.mark.parametrize('suffix', [
    '\ndef broken(value: "Run"):\n    return value\n',
    '\nBroken = list["Run"]\n',
    '\ndef broken():\n    global PUBLIC_STREAM_FIELDS\n    return None\n',
    '\ndef broken():\n    import json\n    return json\n',
    '\nForwardScope = "Scope"\ndef broken(value: ForwardScope):\n    return value\n',
    '\nUnreviewed = (ambient_binding := 1)\n',
])
def test_unsupported_annotation_or_scope_constructs_stop_generation(suffix):
    bad = dict(SOURCES)
    bad['public'] += suffix
    with pytest.raises(ValueError):
        builder.bundle_source(**bad)


@pytest.mark.parametrize('mutation', ['path','loader_order','export','bare_module','sibling_mutation','filesystem','import_alias'])
def test_loader_or_namespace_drift_stops_without_fallback(mutation):
    bad = dict(SOURCES)
    if mutation == 'path':
        bad['producer'] = bad['producer'].replace("with_name('reference.py')","with_name('other.py')")
    elif mutation == 'loader_order':
        old = '_read_spec.loader.exec_module(_read)'
        bad['producer'] = bad['producer'].replace(old,'') + '\n'+old+'\n'
    elif mutation == 'export':
        bad['producer'] += '\nUnreviewed = _read.DoesNotExist\n'
    elif mutation == 'bare_module':
        bad['producer'] += '\nUnreviewed = _read\n'
    elif mutation == 'sibling_mutation':
        bad['producer'] += '\ndef broken():\n    _read.Scope = None\n'
    elif mutation == 'filesystem':
        bad['public'] += '\ndef broken():\n    return open("outside")\n'
    else:
        bad['public'] += '\nimport json as Scope\n'
    with pytest.raises(ValueError):
        builder.bundle_source(**bad)


class JournalObjects:
    """Memory adapter tests serialization only, not real conditional-write behavior."""

    def __init__(self):
        self.data, self.puts = {}, []

    def get(self, role, key):
        assert role == 'artifacts'
        return self.data.get(key)

    def put(self, role, key, raw, expected):
        assert role == 'artifacts'
        prior = self.data.get(key)
        if (prior.etag if prior else None) != expected:
            raise RuntimeError('fake adapter conflict')
        self.puts.append((key, raw, expected))
        self.data[key] = SimpleNamespace(data=raw, etag=str(len(self.puts)))
        return self.data[key].etag


def journal_exercise(symbol, nonce_patch):
    descriptor_raw = WIRE_FIXTURES['producer-descriptor.json']
    descriptor = json.loads(descriptor_raw)
    current_raw = WIRE_FIXTURES['producer-current.json']
    current = json.loads(current_raw)
    args = dict(scope=descriptor['scope'], agent_id=current['agent_id'],
        session_sha256=descriptor['native_session_sha256'], input_sha256=descriptor['input_sha256'],
        admission_ref=descriptor['admission_id'], now=datetime(2026,10,30,0,5,tzinfo=timezone.utc))
    root, key = symbol('admission_keys')(args['scope'], args['agent_id'],
        args['session_sha256'], args['admission_ref'])
    objects = JournalObjects()
    objects.data[root+'descriptor.json'] = SimpleNamespace(data=descriptor_raw, etag='descriptor')
    objects.data[key] = SimpleNamespace(data=current_raw, etag='current')
    nonce_patch(iter(UUID(int=number) for number in range(1000, 1100)))
    lease = symbol('admit_native')(objects, **args)
    stream = descriptor['run']['attempt_id']
    writer = symbol('JournalWriter')(objects, lease, stream, monotonic=lambda:0)
    base = dict(event_id=str(UUID(int=100)), run_id=descriptor['run']['run_id'],
        attempt_id=descriptor['run']['attempt_id'], producer_stream_id=stream,
        producer_sequence=1, span_id=str(UUID(int=70)), parent_span_id=None,
        causation_id=None, emitted_at='2026-10-30T00:05:01Z', type='stage',
        summary='Consulta preparada.', effect='prepared', status='progress')
    writer.append(base)
    assert writer.head.producer_sequence == 1
    text = {**base, 'event_id':str(UUID(int=101)), 'producer_sequence':2,
        'type':'text_delta', 'public_text_delta':'Respuesta pública 🌎'}
    for name in ('summary','effect','status'):
        del text[name]
    writer.append(text)
    assert writer.head.producer_sequence == 2 and writer.pending == []
    assert writer.first_text_flushed
    return (lease.descriptor.model_dump(mode='json'), lease.current.model_dump(mode='json'),
        lease.claim.model_dump(mode='json'), writer.head.model_dump(mode='json'),
        {key:value.data for key,value in objects.data.items()}, objects.puts)


def test_journal_admission_first_text_and_published_head_have_exact_wire_parity(native,monkeypatch):
    namespace,names = native
    if 'journal' not in names:
        return  # Three-source API remains usable without the optional journal.
    def canonical_nonce(sequence):
        monkeypatch.setattr(journal,'uuid4',lambda:next(sequence))
    def native_nonce(sequence):
        monkeypatch.setitem(namespace,'uuid4',lambda:next(sequence))
    expected = journal_exercise(lambda name:getattr(journal,name), canonical_nonce)
    actual = journal_exercise(lambda name:native_symbol(native,'journal',name), native_nonce)
    assert actual == expected
    assert namespace[names['journal']['_read']] is not namespace[names['read']['parse']]


@pytest.mark.parametrize('replacement',[
    ("with_name('producer.py')", "with_name('other.py')"),
    ('p.TypeAdapter(p.Hash64)', 'p.Unknown(p.Hash64)'),
    ('p.TypeAdapter(p.Hash64)', 'p'),
    ('def _hash(value):', 'def _hash(p):'),
    ('return p.TypeAdapter(p.Hash64).validate_python(value)', 'p.Hash64 = value\n    return value'),
])
def test_journal_loader_exports_and_sibling_shadowing_fail_closed(replacement):
    original,changed = replacement
    with pytest.raises(ValueError):
        builder.bundle_source(**SOURCES,journal_source=JOURNAL_SOURCE.replace(original,changed))


def test_optional_journal_bundle_is_deterministic_and_contains_no_loader():
    source = builder.bundle_source(**SOURCES,journal_source=JOURNAL_SOURCE)
    assert source == builder.bundle_source(**SOURCES,journal_source=JOURNAL_SOURCE.replace('\n','\r\n'))
    assert '\r' not in source
    for node in ast.walk(ast.parse(source)):
        if isinstance(node,ast.Name):
            assert node.id not in builder.FORBIDDEN_NAMES | {'p','_read'}
        if isinstance(node,ast.Import):
            assert all(alias.name in ('json','hashlib','copy','inspect','time','math') for alias in node.names)
        if isinstance(node,ast.ImportFrom):
            assert node.module not in ('importlib','pathlib','sys','os')


def result_fixture(kind):
    descriptor = json.loads(WIRE_FIXTURES['producer-descriptor.json'])
    run = descriptor['run']
    payload = {'answer':'Respuesta pública 🌎','sources':[],'publication':{'publication_id':'synthetic'},
        'search_context':{},'query_context':[],'visualization':None}
    if kind != 'chat_response':
        run.update(source='autonomous_study',work_id=str(UUID(int=80)),investigation_id='a'*64)
    if kind == 'native_step':
        payload = dict(work_id=str(UUID(int=80)),input_sha256='b'*64,investigation_id='a'*64,
            phase='report_prepared',report_refs=[{'report_id':'c'*64,'revision':1}],evaluation_count=3,
            request_count=2,proposal_count=0,publication_status='pending')
    if kind == 'gold_report':
        run.update(execution_kind='gold_publication',component='aidp_gold_runner',agent_id=None,agent_name=None)
        payload = {'reports':[{'summary':'Informe sintético','status':'completed_with_limits'}],
            'publication':{'id':'synthetic'}}
    envelope = dict(schema_version=results.RESULT_VERSION,scope=descriptor['scope'],result_id=str(UUID(int=50)),
        run_id=run['run_id'],attempt_id=run['attempt_id'],kind=kind,payload=payload)
    return envelope,run


@pytest.mark.parametrize('kind',['chat_response','native_step','gold_report'])
def test_five_source_result_encoding_parsing_has_exact_wire_parity(native,kind):
    namespace,names = native
    if 'results' not in names:
        return
    envelope,run = result_fixture(kind)
    raw = results.result_bytes(envelope)
    assert native_symbol(native,'results','result_bytes')(envelope) == raw
    ref = {'result_id':envelope['result_id'],'sha256':producer.producer_sha256(raw),'byte_length':len(raw)}
    calls=[]
    def semantic_stub(payload,*,scope,run):
        # Fixture callback only: this does not certify application business data.
        calls.append((copy.deepcopy(payload),scope,run))
        return True
    args=dict(expected_scope=envelope['scope'],expected_run=run,expected_ref=ref,
        chat_validator=semantic_stub,gold_report_validator=semantic_stub,expected_work_input_sha256='b'*64)
    canonical = results.parse_result(raw,**args).model_dump(mode='json')
    bundled = native_symbol(native,'results','parse_result')(raw,**args).model_dump(mode='json')
    assert canonical == bundled == envelope
    assert len(calls) == (0 if kind=='native_step' else 2)
    if calls:
        assert calls[0] == calls[1]
    assert namespace['Scope'] is not namespace[names['results']['ResultEnvelope']].model_fields['scope'].annotation


def normalize_private_schema(value):
    if isinstance(value,dict):
        return {normalize_private_schema(key):normalize_private_schema(item) for key,item in value.items()}
    if isinstance(value,list):
        return [normalize_private_schema(item) for item in value]
    if isinstance(value,str):
        for prefix in builder.PREFIXES.values():
            value=value.replace(prefix,'')
    return value


def test_five_source_result_schema_preserves_every_wire_field(native):
    if 'results' not in native[1]:
        return
    actual=native_symbol(native,'results','RESULTS').json_schema()
    assert normalize_private_schema(actual)==results.RESULTS.json_schema()


@pytest.mark.parametrize('change',[
    ("with_name('producer.py')","with_name('other.py')"),
    ("'_sonda_activity_results_producer'","'_unreviewed_loader'"),
    ('p.Hash64','p.Unknown'),
    ('model = RESULTS.validate_python(value)','import os\n    model = RESULTS.validate_python(value)'),
    ('model = RESULTS.validate_python(value)','unreviewed_ambient(value)\n    model = RESULTS.validate_python(value)'),
    ('def result_bytes(value):','def result_bytes(p):'),
])
def test_results_fixed_loader_and_scopes_reject_unreviewed_changes(change):
    original,modified=change
    with pytest.raises(ValueError):
        builder.bundle_source(**SOURCES,journal_source=JOURNAL_SOURCE,
            results_source=RESULTS_SOURCE.replace(original,modified))


def test_results_option_does_not_change_previous_three_or_four_source_bytes():
    import hashlib
    assert hashlib.sha256(builder.bundle_source(**SOURCES).encode()).hexdigest() == '389aaabbad41d342b9208d5e7ccdedff87a964d2740b9d34067aac31478a9856'
    assert hashlib.sha256(builder.bundle_source(**SOURCES,journal_source=JOURNAL_SOURCE).encode()).hexdigest() == 'c45346d54446ab786d0a6b3f6de56fac3c054aa133cf99523701d30281b8f221'
    assert builder.bundle_source(**SOURCES,results_source=None)==builder.bundle_source(**SOURCES)


def test_result_codec_can_be_included_without_optional_journal():
    source=builder.bundle_source(**SOURCES,results_source=RESULTS_SOURCE)
    names=builder.binding_map(**SOURCES,results_source=RESULTS_SOURCE)
    assert 'results' in names and 'journal' not in names
    assert source==builder.bundle_source(**SOURCES,results_source=RESULTS_SOURCE.replace('\n','\r\n'))
    assert 'ActivityResult_parse_result' in source
    assert 'importlib' not in source
