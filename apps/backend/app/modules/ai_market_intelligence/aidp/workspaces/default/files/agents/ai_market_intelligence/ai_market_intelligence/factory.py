"""Evaluate the portable current graph over frozen captured documents, without managed business tables."""
import copy
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tomllib


from .model import DecisionModel
from .runtime_core import Conflict, Scope
from .runtime_versions import exact_runtime_comparison, runtime_versions
from .state import MarketRunStore
from .workflow import MarketWorkflow
from .collector import MarketCollector
from .native import market_question_urls
from .social import social_url
from .runtime_core import public_url
from .model_bridge import StructuredBridge


class MarketEvaluationSnapshot:
    maximum_bytes=4*1024*1024

    def __init__(self,scope):
        self.scope=scope
        self.value=None
        self.revision=0

    def read(self):
        return copy.deepcopy(self.value),self.revision

    def write(self,value,etag):
        if etag!=self.revision:
            raise Conflict('Frozen evaluation state changed concurrently')
        self.value=copy.deepcopy(value)
        self.revision+=1


class MarketEvaluationStore(MarketRunStore):
    def __init__(self,snapshot,question,revision):
        super().__init__(snapshot,revision)
        self.question=question

    def begin(self,request_id,question,**kwargs):
        if question!=self.question:
            raise ValueError('This frozen document set belongs to a different question')
        return super().begin(request_id,question,**kwargs)


class MarketFrozenCollector:
    def __init__(self,rows,*,explicit_urls=()):
        if any(count>50 for count in Counter(row['source'] for row in rows).values()):
            raise ValueError('Frozen source exceeds fifty documents; partition and review the corpus before inference')
        self.rows=copy.deepcopy(rows)
        self.calls=[]
        self.authority=MarketCollector(None,None,explicit_urls=explicit_urls)

    async def collect(self,**arguments):
        self.calls.append(copy.deepcopy(arguments))
        source=arguments['task']['source']
        rows=[row for row in self.rows if row['source']==source]
        urls=arguments['task']['urls']
        if urls and not self.authority.explicit_targets_match(source,urls):
            raise ValueError('Frozen evaluation cannot retrieve a proposed or missing destination')
        if urls:
            canonical=lambda value:social_url(value,source) if source in {'facebook','instagram'} else public_url(value)
            selected={canonical(url) for url in urls}
            rows=[row for row in rows if selected&{canonical(url) for url in (row['url'],row.get('requested_url')) if url}]
        return {'status':'complete' if rows else 'unavailable','evidence':rows,
                'limitations':['Evaluación sobre documentos congelados; no se ejecutaron las consultas propuestas.'],
                'operation_id':arguments['operation_key']}


def market_evaluation_graph(frozen,model,checkpointer,*,revision,conversation_context=None,enabled_sources=None):
    if frozen.get('version')!=2 or not isinstance(frozen.get('evidence'),dict) or not frozen.get('brief'):
        raise ValueError('A captured current request and its evidence inventory are required')
    context=copy.deepcopy(conversation_context or {})
    prior=context.pop('prior_report',None)
    if not frozen['evidence'] and not frozen['brief'].get('clarification') and frozen['brief'].get('intent','research')=='research':
        raise ValueError('Frozen research requires captured documents; absence is not a positive reasoning control')
    sources=frozen['brief']['sources'] or (prior['brief']['sources'] if prior else enabled_sources)
    snapshot=MarketEvaluationSnapshot(Scope(**frozen['scope']))
    store=MarketEvaluationStore(snapshot,frozen['question'],revision)
    collector=MarketFrozenCollector(list(frozen['evidence'].values()),explicit_urls=market_question_urls(frozen['question']))
    workflow=MarketWorkflow(store,model,collector,enabled_sources=sources,
        clock=lambda:datetime.fromisoformat(frozen['started_at']),conversation_context=context,prior_report=prior)
    return workflow.build(checkpointer),store,collector


def market_checked_json(root,relative,checksum):
    path=(root/relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.is_symlink():
        raise ValueError('Evaluation input must stay in its owning project')
    raw=path.read_bytes()
    if len(raw)>4*1024*1024 or hashlib.sha256(raw).hexdigest()!=checksum:
        raise ValueError('Frozen evaluation input changed or exceeds its limit')
    return json.loads(raw)


def build_graph(runtime):
    if runtime['external_effects_allowed'] is not False:
        raise ValueError('Frozen current evaluation cannot start external tools')
    if type(runtime['model']).__name__=='FixtureModel':
        raise ValueError('Use the separate synthetic fixture factory for FixtureModel')
    if getattr(runtime['model'],'client',None) is not None:
        raise ValueError('Current evaluation requires a fresh model client whose identity and guardrails can be verified')
    root=Path.cwd()
    config=tomllib.loads((root/'aidp-agent.toml').read_text(encoding='utf-8'))
    evaluation=config['market_test']
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=evaluation['factory_sha256']:
        raise ValueError('The current evaluation factory changed; review its binding')
    adapter=Path(__file__).with_name('model_bridge.py')
    if hashlib.sha256(adapter.read_bytes()).hexdigest()!=evaluation['adapter_sha256']:
        raise ValueError('The shared model adapter changed; review its binding')
    entry=(root/config['agent']['entry']).resolve()
    if not entry.is_relative_to(root.resolve()) or not entry.is_file() or entry.is_symlink():
        raise ValueError('The evaluation candidate must stay in its owning project')
    candidate=json.loads(entry.with_name('agent-manifest.json').read_text(encoding='utf-8'))
    if (candidate.get('schema_version')!=1 or candidate.get('candidate_revision')!=evaluation['candidate_revision']
            or hashlib.sha256(entry.read_bytes()).hexdigest()!=candidate.get('source_sha256')):
        raise ValueError('The evaluation candidate differs from the current runtime package')
    frozen=market_checked_json(root,evaluation['snapshot_file'],evaluation['snapshot_sha256'])
    binding=market_checked_json(root,evaluation['binding_file'],evaluation['binding_sha256'])
    if binding.get('candidate_revision')!=evaluation['candidate_revision']:
        raise ValueError('The service binding differs from the evaluation candidate')
    profile=binding['runtime_profile']
    if not exact_runtime_comparison(runtime_versions(tuple(profile['packages'])),profile)['exact_match']:
        raise ValueError('Local evaluation does not match the observed AIDP runtime')
    for key in ('model_id','region','compartment_id'):
        if config['model'][key]!=binding[key]:
            raise ValueError('Local/native model bindings differ')
    prompts=Path(__file__).with_name('prompts.json')
    model=DecisionModel(StructuredBridge(runtime['model'],binding,
        observations_dir=root/'.aidp-local/market/structured-responses'),prompts=json.loads(prompts.read_text(encoding='utf-8')))
    context={'scope_defaults':binding['search_context']}
    if evaluation.get('context_file'):
        context.update(market_checked_json(root,evaluation['context_file'],evaluation['context_sha256']))
    graph,_,_=market_evaluation_graph(frozen,model,runtime['checkpointer'],revision=evaluation['candidate_revision'],
                               conversation_context=context,enabled_sources=binding['enabled_sources'])
    return graph
